"""Production adapter for HygieneGate. Flag default off. Does not write skills.

``recertia.rsi`` stays free of the episodic store, the hash chain, and the
container. This module is the only place that calls ``run_in_container``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from contracts.run import ReusabilityVerdict, RunState
from recertia.ledger.hashchain import HashChainLedger
from recertia.memory.episodic import CaseRecord, DeadEnd, EpisodicStore
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.rsi.hygiene import HygieneGate
from recertia.rsi.models import HYGIENE_REASONS, ExecResult, HygieneRequest, HygieneResult, ScreenedArtifact
from recertia.rsi.world import World

DEFAULT_POLICY = "sandbox-default"
ALLOWED_POLICIES = frozenset({DEFAULT_POLICY})
_HOST_TOKENS = ("/home/", "/Users/", "/root/", "C:\\")
_PRIVATE_TEXT = ("PRIV_GUIDANCE", "SYSTEM_PROMPT:", "<<SYS>>")

GateExecutor = Callable[[dict], ExecResult]
ContainerRunner = Callable[..., Any]


def hygiene_enabled() -> bool:
    """True only when the process opted in. Unset and ``0`` stay off."""

    return os.environ.get("RECERTIA_HYGIENE_ENABLED") == "1"


@dataclass
class HygieneHold:
    """``outcome`` set means distill must stop. ``artifact_id`` is an accepted ``scr_`` id."""

    outcome: NodeOutcome | None
    artifact_id: str | None = None


def production_executor(
    public: dict,
    *,
    timeout_s: float | None,
    policy_id: str,
    workdir: Path,
    runner: ContainerRunner | None = None,
    allowed_policies: frozenset[str] = ALLOWED_POLICIES,
) -> ExecResult:
    """Re-execute the public prefix. A private bag is a programming error.

    Missing budget, an unknown policy, or no command returns ``sandbox_error``
    and does not call ``runner``. ``run_sandboxed`` is never used.
    """

    _assert_public(public)
    if timeout_s is None or timeout_s <= 0 or int(timeout_s) <= 0:
        return _error_result("sandbox_error")
    if policy_id not in allowed_policies:
        return _error_result("sandbox_error")
    command = _command_from(public)
    if not command:
        return _error_result("sandbox_error")
    if runner is None:
        return _container_runner(command, workdir=workdir, timeout_s=float(timeout_s))
    try:
        outcome = runner(command, workdir=workdir, timeout_s=float(timeout_s))
    except subprocess.TimeoutExpired:
        return _error_result("timeout")
    except Exception as exc:
        from recertia.solver.sandbox import SandboxError

        if isinstance(exc, SandboxError):
            return _error_result("sandbox_error")
        raise
    if isinstance(outcome, ExecResult):
        return outcome
    return _from_completed(command, outcome, elapsed_s=0.0)


def run_durable_hygiene(
    req: HygieneRequest,
    *,
    store: EpisodicStore,
    ledger: HashChainLedger,
    executor: GateExecutor,
) -> HygieneResult:
    """Run the gate once. A second process with the same key does not execute."""

    replayed = _replay(ledger, store, req.idempotency_key)
    if replayed is not None:
        return replayed
    world = World(job_id="rsi-bridge")
    world.register_criteria(req.criteria_hash, req.task_class, ["locked"])
    _preload_screened(world, ledger)
    result = HygieneGate(world, executor).run(req)
    return _persist(store, ledger, world, req, result)


def live_hygiene_block(
    state: RunState,
    ctx: NodeContext,
    *,
    runner: ContainerRunner | None = None,
) -> HygieneHold | None:
    """None when the flag is off. A hold with an outcome is a distill refusal."""

    if not hygiene_enabled():
        return None
    if ctx.episodic is None:
        return HygieneHold(outcome=_one_off(state, "hygiene gate requires episodic store"))
    timeout_s = _solve_budget(state)
    public = _public_from_state(state, ctx)
    criteria_hash = state.manifest.criteria_hash or _criteria_hash(list(state.criteria))
    req = HygieneRequest(
        criteria_hash=criteria_hash,
        task_class=state.task.task_class or "unset",
        sandbox_policy_id=DEFAULT_POLICY,
        idempotency_key=f"distill:{state.run_id}:{state.attempt_no}",
        import_payload=public,
    )
    ledger = _ledger_for(ctx.ledger, ctx.episodic.root)

    def executor(view: dict) -> ExecResult:
        return production_executor(
            view,
            timeout_s=timeout_s,
            policy_id=req.sandbox_policy_id,
            workdir=ctx.workdir,
            runner=runner,
        )

    result = run_durable_hygiene(req, store=ctx.episodic, ledger=ledger, executor=executor)
    if not result.accepted or not result.artifact_id:
        return HygieneHold(outcome=_one_off(state, f"hygiene rejected: {result.reason}"))
    return HygieneHold(outcome=None, artifact_id=result.artifact_id)


def gate_import(
    imported: Any,
    *,
    task_class: str,
    runs_root: Path,
    executor: GateExecutor | None = None,
) -> HygieneResult:
    """Screen one imported trajectory before ``distill_imported`` may author."""

    public = public_import(imported, task_class=task_class)
    criteria = list(getattr(imported, "criteria_snapshot", []) or [])
    criteria_hash = _criteria_hash(criteria)
    timeout_s = _timeouts(criteria)
    req = HygieneRequest(
        criteria_hash=criteria_hash,
        task_class=task_class,
        sandbox_policy_id=DEFAULT_POLICY,
        idempotency_key=f"import:{imported.import_id}:{criteria_hash[:16]}",
        import_payload=public,
    )
    store = EpisodicStore(runs_root / "episodic")
    ledger = HashChainLedger(runs_root / "ledger" / "ledger.jsonl")
    workdir = runs_root / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    def default_executor(view: dict) -> ExecResult:
        return production_executor(
            view,
            timeout_s=timeout_s,
            policy_id=req.sandbox_policy_id,
            workdir=workdir,
        )

    return run_durable_hygiene(
        req,
        store=store,
        ledger=ledger,
        executor=executor or default_executor,
    )


def public_import(imported: Any, *, task_class: str) -> dict:
    """Commands and the import id only. The private bag is not a field here."""

    commands: list[str] = []
    for step in getattr(imported, "steps", []) or []:
        cmd = (getattr(step, "input", None) or getattr(step, "action", None) or "").strip()
        if cmd:
            commands.append(cmd)
    return {
        "import_id": imported.import_id,
        "task_class": task_class,
        "commands": commands,
        "cmd": commands[0] if commands else "",
    }


def point_provenance(draft: Any, artifact_id: str) -> Any:
    """Record the screened id on an existing provenance list. No schema field is added."""

    ids = list(draft.provenance.source_case_ids)
    if artifact_id not in ids:
        ids.append(artifact_id)
    provenance = draft.provenance.model_copy(update={"source_case_ids": ids})
    return draft.model_copy(update={"provenance": provenance})


def _public_from_state(state: RunState, ctx: NodeContext) -> dict:
    from recertia.nodes.distill import _commands_from_context

    return {
        "request": state.task.request or "",
        "commands": list(_commands_from_context(state, ctx)),
    }


def _solve_budget(state: RunState) -> float | None:
    return _timeouts(list(state.criteria))


def _timeouts(criteria: list) -> float | None:
    found: list[float] = []
    for item in criteria:
        timeout = getattr(item, "timeout_s", None)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            continue
        if timeout > 0:
            found.append(float(timeout))
    if not found:
        return None
    return min(found)


def _criteria_hash(criteria: list) -> str:
    payload = []
    for item in criteria:
        if hasattr(item, "model_dump"):
            payload.append(item.model_dump(mode="json"))
        else:
            payload.append(item)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _one_off(state: RunState, reason: str) -> NodeOutcome:
    verdict = ReusabilityVerdict(
        verdict="one_off",
        parameterisable=False,
        context_free=True,
        checkable=True,
        not_duplicate=True,
        bounded=True,
        reason=reason,
    )
    new_state = state.model_copy(update={"reusability": verdict, "draft": None, "facts_extracted": []})
    return NodeOutcome(state=new_state, route="one_off", note=reason)


def _ledger_for(ledger: Any, episodic_root: Path) -> HashChainLedger:
    if isinstance(ledger, HashChainLedger):
        return ledger
    path = getattr(ledger, "path", None)
    if isinstance(path, Path):
        return HashChainLedger(path)
    return HashChainLedger(episodic_root.parent / "ledger" / "ledger.jsonl")


def _assert_public(public: dict) -> None:
    if _has_key(public, "private"):
        raise ValueError("private bag forwarded to sandbox")
    for text in _strings(public):
        if any(token in text for token in _PRIVATE_TEXT):
            raise ValueError("private material forwarded to sandbox")
        if any(token in text for token in _HOST_TOKENS):
            raise ValueError("host path forwarded to sandbox")


def _has_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_has_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_has_key(item, key) for item in value)
    return False


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.append(str(key))
            found.extend(_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_strings(item))
        return found
    return []


def _command_from(public: dict) -> str | None:
    inner = public.get("public") if isinstance(public.get("public"), dict) else public
    if not isinstance(inner, dict):
        return None
    cmd = inner.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip()
    commands = inner.get("commands")
    if isinstance(commands, list):
        parts = [str(item).strip() for item in commands if str(item).strip()]
        if parts:
            return " && ".join(parts)
    return None


def _error_result(error: str, elapsed: float = 0.0) -> ExecResult:
    return ExecResult(
        fresh_execution_id="fx-none",
        actions=[],
        observations=[],
        turns=[],
        checks_passed=0,
        checks_total=0,
        strict_success=False,
        elapsed_s=elapsed,
        error=error,
    )


def _container_runner(command: str, *, workdir: Path, timeout_s: float) -> ExecResult:
    from recertia.solver.container import run_in_container
    from recertia.solver.sandbox import SandboxError

    started = time.monotonic()
    try:
        proc = run_in_container(command, workdir=workdir, timeout_s=int(timeout_s))
    except subprocess.TimeoutExpired:
        return _error_result("timeout", elapsed=time.monotonic() - started)
    except SandboxError:
        return _error_result("sandbox_error", elapsed=time.monotonic() - started)
    return _from_completed(command, proc, elapsed_s=time.monotonic() - started)


def _from_completed(command: str, proc: Any, *, elapsed_s: float) -> ExecResult:
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    code = getattr(proc, "returncode", 1)
    digest = hashlib.sha256(f"{command}\n{stdout}\n{stderr}".encode()).hexdigest()[:16]
    if code != 0:
        return ExecResult(
            fresh_execution_id=f"fx-{digest}",
            actions=[command],
            observations=[(stderr or stdout)[:500]],
            turns=[],
            checks_passed=0,
            checks_total=1,
            strict_success=False,
            elapsed_s=elapsed_s,
            error="sandbox_error",
        )
    return ExecResult(
        fresh_execution_id=f"fx-{digest}",
        actions=[command],
        observations=[stdout],
        turns=[],
        checks_passed=1,
        checks_total=1,
        strict_success=True,
        elapsed_s=elapsed_s,
    )


def _replay(ledger: HashChainLedger, store: EpisodicStore, key: str) -> HygieneResult | None:
    entries = getattr(ledger, "entries", None)
    if callable(entries):
        found: dict | None = None
        for entry in entries():
            evidence = entry.evidence or {}
            if evidence.get("kind") == "rsi_idempotency" and evidence.get("idempotency_key") == key:
                payload = evidence.get("result")
                if isinstance(payload, dict):
                    found = payload
        if found is not None:
            restored = _result_from(found)
            if restored is not None:
                return restored
    case = store.get_by_case_id(_idem_case_id(key))
    if case is None or not case.approach:
        return None
    try:
        payload = json.loads(case.approach)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("idempotency_key") != key:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    return _result_from(result)


def _result_from(payload: dict) -> HygieneResult | None:
    reason = payload.get("reason")
    if reason not in HYGIENE_REASONS:
        return None
    return HygieneResult(
        accepted=bool(payload.get("accepted")),
        reason=str(reason),
        ledger_receipt=str(payload.get("ledger_receipt") or ""),
        checks_passed=int(payload.get("checks_passed") or 0),
        checks_total=int(payload.get("checks_total") or 0),
        strict_success=bool(payload.get("strict_success")),
        artifact_id=_opt_str(payload.get("artifact_id")),
        fresh_execution_id=_opt_str(payload.get("fresh_execution_id")),
        duplicate_of=_opt_str(payload.get("duplicate_of")),
    )


def _opt_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _result_dict(result: HygieneResult) -> dict[str, Any]:
    return {
        "accepted": result.accepted,
        "reason": result.reason,
        "ledger_receipt": result.ledger_receipt,
        "checks_passed": result.checks_passed,
        "checks_total": result.checks_total,
        "strict_success": result.strict_success,
        "artifact_id": result.artifact_id,
        "fresh_execution_id": result.fresh_execution_id,
        "duplicate_of": result.duplicate_of,
    }


def _preload_screened(world: World, ledger: HashChainLedger) -> None:
    entries = getattr(ledger, "entries", None)
    if not callable(entries):
        return
    for entry in entries():
        evidence = entry.evidence or {}
        if evidence.get("kind") != "rsi_screened":
            continue
        artifact = _artifact_from(evidence, entry.entry_hash)
        if artifact is None:
            continue
        parent = artifact.parent_trajectory_id or artifact.import_hash
        accept_key = (parent, artifact.criteria_hash, artifact.payload_sha256)
        world.screened[artifact.artifact_id] = artifact
        world.accepted_keys[accept_key] = artifact.artifact_id


def _artifact_from(evidence: dict, receipt: str) -> ScreenedArtifact | None:
    artifact_id = _opt_str(evidence.get("artifact_id"))
    digest = _opt_str(evidence.get("payload_sha256"))
    criteria_hash = _opt_str(evidence.get("criteria_hash"))
    task_class = _opt_str(evidence.get("task_class"))
    fresh = _opt_str(evidence.get("fresh_execution_id"))
    parent = _opt_str(evidence.get("parent_trajectory_id"))
    import_hash = _opt_str(evidence.get("import_hash"))
    if not artifact_id or not digest or not criteria_hash or not task_class or not fresh:
        return None
    if bool(parent) == bool(import_hash):
        return None
    raw_bytes = evidence.get("payload_bytes")
    if isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int):
        return None
    return ScreenedArtifact(
        artifact_id=artifact_id,
        criteria_hash=criteria_hash,
        task_class=task_class,
        fresh_execution_id=fresh,
        payload_uri=str(evidence.get("payload_uri") or f"blobs/{digest}.json.gz"),
        payload_sha256=digest,
        payload_bytes=raw_bytes,
        checks_passed=int(evidence.get("checks_passed") or 0),
        checks_total=int(evidence.get("checks_total") or 0),
        strict_success=True,
        leakage_scan_version=str(evidence.get("leakage_scan_version") or "leak-v1"),
        producing_job_id="rsi-bridge",
        ledger_receipt=receipt,
        created_at=_now(),
        parent_trajectory_id=parent,
        import_hash=import_hash,
    )


def _persist(
    store: EpisodicStore,
    ledger: HashChainLedger,
    world: World,
    req: HygieneRequest,
    result: HygieneResult,
) -> HygieneResult:
    parent, import_hash = _identity(req)
    if result.accepted and result.artifact_id:
        artifact = world.screened[result.artifact_id]
        _write_blob(store, world, artifact)
        _write_case(
            store,
            case_id=artifact.artifact_id,
            run_id=req.idempotency_key,
            task_class=req.task_class,
            outcome="solved",
            excerpt=f"sha256:{artifact.payload_sha256[:16]}",
            transcript_ref=f"blobs/{artifact.payload_sha256}.json.gz",
            failure_class=None,
            why=None,
        )
        receipt = _append(
            ledger,
            target=artifact.artifact_id,
            evidence={
                "kind": "rsi_screened",
                "artifact_id": artifact.artifact_id,
                "payload_sha256": artifact.payload_sha256,
                "payload_uri": f"blobs/{artifact.payload_sha256}.json.gz",
                "payload_bytes": artifact.payload_bytes,
                "criteria_hash": artifact.criteria_hash,
                "task_class": artifact.task_class,
                "fresh_execution_id": artifact.fresh_execution_id,
                "checks_passed": artifact.checks_passed,
                "checks_total": artifact.checks_total,
                "strict_success": True,
                "leakage_scan_version": artifact.leakage_scan_version,
                "parent_trajectory_id": parent,
                "import_hash": import_hash,
                "stores_body": False,
            },
        )
        result = HygieneResult(
            accepted=True,
            reason="ok",
            ledger_receipt=receipt,
            checks_passed=result.checks_passed,
            checks_total=result.checks_total,
            strict_success=True,
            artifact_id=result.artifact_id,
            fresh_execution_id=result.fresh_execution_id,
        )
    else:
        sha = ""
        case_id = ""
        if result.duplicate_of and result.duplicate_of in world.screened:
            sha = world.screened[result.duplicate_of].payload_sha256
        if world.negatives:
            negative = list(world.negatives.values())[-1]
            case_id = negative.case_id
            sha = sha or negative.payload_sha256
        if not case_id:
            raw = f"{result.reason}|{req.idempotency_key}|{sha}"
            case_id = "neg_" + hashlib.sha256(raw.encode()).hexdigest()[:16]
        _write_case(
            store,
            case_id=case_id,
            run_id=req.idempotency_key,
            task_class=req.task_class,
            outcome="failed",
            excerpt=f"{result.reason}:{sha[:12]}",
            transcript_ref=None,
            failure_class=result.reason,
            why=result.reason,
        )
        receipt = _append(
            ledger,
            target=case_id,
            evidence={
                "kind": "rsi_negative",
                "case_id": case_id,
                "reason": result.reason,
                "criteria_hash": req.criteria_hash,
                "payload_sha256": sha,
                "stores_body": False,
                "idempotency_key": req.idempotency_key,
            },
        )
        result = HygieneResult(
            accepted=False,
            reason=result.reason,
            ledger_receipt=receipt,
            checks_passed=result.checks_passed,
            checks_total=result.checks_total,
            strict_success=False,
            artifact_id=None,
            fresh_execution_id=result.fresh_execution_id,
            duplicate_of=result.duplicate_of,
        )
    body = {"idempotency_key": req.idempotency_key, "result": _result_dict(result)}
    _append(
        ledger,
        target=req.idempotency_key,
        evidence={
            "kind": "rsi_idempotency",
            "idempotency_key": req.idempotency_key,
            "result": body["result"],
        },
    )
    if store.get_by_case_id(_idem_case_id(req.idempotency_key)) is None:
        store.write(
            CaseRecord(
                case_id=_idem_case_id(req.idempotency_key),
                run_id=req.idempotency_key,
                attempt_no=0,
                task_class=req.task_class,
                request_excerpt="rsi-idempotency",
                outcome="abandoned",
                approach=json.dumps(body, sort_keys=True),
            )
        )
    return result


def _write_blob(store: EpisodicStore, world: World, artifact: ScreenedArtifact) -> None:
    blob = world.blobs.get(artifact.payload_uri)
    if not isinstance(blob, bytes):
        raise RuntimeError("accepted hygiene result has no payload blob")
    dest = store.root / "blobs" / f"{artifact.payload_sha256}.json.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(blob)


def _write_case(
    store: EpisodicStore,
    *,
    case_id: str,
    run_id: str,
    task_class: str,
    outcome: str,
    excerpt: str,
    transcript_ref: str | None,
    failure_class: str | None,
    why: str | None,
) -> None:
    if store.get_by_case_id(case_id) is not None:
        return
    dead = DeadEnd(approach="rsi-hygiene", why_failed=why) if why else None
    store.write(
        CaseRecord(
            case_id=case_id,
            run_id=run_id,
            attempt_no=0,
            task_class=task_class,
            request_excerpt=excerpt[:200],
            outcome=outcome,  # type: ignore[arg-type]
            failure_class=failure_class,
            dead_end=dead,
            transcript_ref=transcript_ref,
            approach="rsi-hygiene",
        )
    )


def _append(ledger: HashChainLedger, *, target: str, evidence: dict) -> str:
    entry = ledger.append(actor="rsi-bridge", action="write", target=target, evidence=evidence)
    return str(entry.entry_hash)


def _identity(req: HygieneRequest) -> tuple[str | None, str | None]:
    if req.trajectory_id is not None:
        return req.trajectory_id, None
    raw = json.dumps(req.import_payload, sort_keys=True, separators=(",", ":"), default=str)
    return None, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _idem_case_id(key: str) -> str:
    return "idem_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
