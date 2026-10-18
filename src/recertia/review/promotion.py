"""Promote a skill to ``approved`` only after its golden regression gate passes (specs §8).

The regression runner's log is the evidence — recorded on ``SkillStatus.certification`` —
not a note in a PR description. M4: the gate may run the full task-class harness and must
name every failing fixture id on refusal.

Successor versions additionally run every golden fixture the predecessor passed. A candidate
that still "solves" its own fixture but fails a predecessor fixture is a non-regression
refusal — competence compounds; it does not replace.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.skill import SkillVersion
from contracts.status import Certification, SkillStatus
from recertia.memory.procedural.active_set import assign_active_on_approval
from recertia.memory.procedural.lint import lint_skill
from recertia.memory.procedural.store import SkillStore
from recertia.ports.orchestration import GateRunner
from recertia.review.gate import discover_golden, discover_version_golden


class PromotionWriter:
    """The only promotion path that persists ``lifecycle=approved``."""

    def __init__(self, store: SkillStore) -> None:
        self._store = store

    def write(self, status: SkillStatus) -> Path:
        if status.lifecycle != "approved":
            raise PromotionError(f"PromotionWriter refuses lifecycle={status.lifecycle!r}")
        return self._store._write_status_unchecked(status)


class PromotionError(Exception):
    """Golden gate failed, or the skill is not yet eligible for approval."""

    def __init__(self, message: str, *, failing_fixtures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failing_fixtures = failing_fixtures or []


def promote_to_approved(
    store: SkillStore,
    skill_id: str,
    version: int,
    *,
    golden_dir: Path | None = None,
    golden_root: Path | None = None,
    runs_root: Path,
    log_dir: Path,
    model_validated_on: str = "m1-seed",
    tool_fingerprint: dict[str, str] | None = None,
    repo_root: Path | None = None,
    require_task_class_gate: bool = False,
    factory: object | None = None,
    executor: object | None = None,
    gate: GateRunner | None = None,
) -> SkillStatus:
    """Run golden regression; on pass, write ``lifecycle=approved`` and the log ref.

    Prefer ``golden_root`` + task-class harness when ``require_task_class_gate`` is set (M4).
    Otherwise a single ``golden_dir`` preserves the M1 seed-promotion path.

    ``self_distilled`` versions are approved but stay inactive until live-mix admission
    (contribution evidence); human-authored and mined versions go active on approval.
    """

    ver = store.get_version(skill_id, version)
    status = store.get_status(skill_id, version)
    stats = store.get_stats(skill_id, version)

    candidate_status = status.model_copy(update={"lifecycle": "candidate"})
    violations = lint_skill(ver, candidate_status, stats, store=store)
    if violations:
        raise PromotionError(f"skill not eligible for approval: {violations}")

    from recertia.memory.procedural.applicability import refuse_if_inapplicable

    applicability = refuse_if_inapplicable(ver, store=store)
    if not applicability.ok:
        reasons = [r.message for r in applicability.reasons]
        raise PromotionError(f"applicability gate refused {skill_id}@v{version}: {reasons}")

    extra_dirs = _predecessor_golden_dirs(
        store,
        ver,
        golden_root=golden_root,
        golden_dir=golden_dir,
        log_dir=log_dir,
        repo_root=repo_root,
    )

    report = _run_regression(
        ver,
        golden_dir=golden_dir,
        golden_root=golden_root,
        runs_root=runs_root,
        require_task_class_gate=require_task_class_gate,
        extra_golden_dirs=extra_dirs,
        factory=factory,
        executor=executor,
        gate=gate,
    )

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{skill_id}-v{version}.json"
    log_path.write_text(_report_json(report), encoding="utf-8")

    regressions = _predecessor_regressions(extra_dirs, report)
    if not report.all_passed or regressions:
        failing = [
            Path(r.golden_path).name or r.skill_id for r in report.results if not r.passed
        ]
        for name in regressions:
            if name not in failing:
                failing.append(name)
        kind = "predecessor non-regression" if regressions else "golden gate"
        raise PromotionError(
            f"{kind} failed for {skill_id}@v{version}: "
            f"failing fixtures={failing} (log: {log_path})",
            failing_fixtures=failing,
        )

    golden_ref = _portable_ref(log_path, repo_root)

    approved = SkillStatus(
        skill_id=skill_id,
        version=version,
        lifecycle="approved",
        active=False,
        certification=Certification(
            model_validated_on=model_validated_on,
            tool_fingerprint=tool_fingerprint or {},
            golden_set_ref=golden_ref,
            last_recertified_at=datetime.now(timezone.utc),
            recert_status="fresh",
        ),
    )
    approved = assign_active_on_approval(approved, version=ver, stats=stats)
    violations = lint_skill(ver, approved, stats, store=store)
    if violations:
        raise PromotionError(f"approved profile violations: {violations}")
    # Refuse promotion if a concurrent quarantine/bench landed during the golden gate.
    current = store.get_status(skill_id, version)
    if current.lifecycle in ("quarantined", "retired", "benched", "needs_recert"):
        raise PromotionError(
            f"promotion aborted: lifecycle moved to {current.lifecycle!r} during golden gate"
        )
    PromotionWriter(store).write(approved)
    return approved


def predecessor_version(store: SkillStore, version: SkillVersion) -> SkillVersion | None:
    """The version this candidate supersedes, if it exists on disk."""

    if version.supersedes is not None:
        try:
            return store.get_version(version.skill_id, version.supersedes)
        except FileNotFoundError:
            return None
    prior: list[int] = []
    for skill_id, ver_n in store.list_versions():
        if skill_id != version.skill_id or ver_n >= version.version:
            continue
        try:
            status = store.get_status(skill_id, ver_n)
        except FileNotFoundError:
            continue
        if status.lifecycle == "approved":
            prior.append(ver_n)
    if not prior:
        return None
    return store.get_version(version.skill_id, max(prior))


def _predecessor_golden_dirs(
    store: SkillStore,
    version: SkillVersion,
    *,
    golden_root: Path | None,
    golden_dir: Path | None,
    log_dir: Path,
    repo_root: Path | None,
) -> list[Path]:
    pred = predecessor_version(store, version)
    if pred is None:
        return []
    try:
        pred_status = store.get_status(pred.skill_id, pred.version)
    except FileNotFoundError:
        pred_status = None

    dirs: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        dirs.append(path)

    if pred_status is not None:
        for path in _passed_dirs_from_log(
            pred_status.certification.golden_set_ref,
            log_dir=log_dir,
            repo_root=repo_root,
            skill_id=pred.skill_id,
            version=pred.version,
        ):
            _add(path)

    inferred_root = golden_root
    if inferred_root is None and golden_dir is not None:
        if golden_dir.parent.name == version.task_class:
            inferred_root = golden_dir.parent.parent
        elif golden_dir.parent.parent.name == version.task_class:
            inferred_root = golden_dir.parent.parent.parent
    if inferred_root is not None:
        versioned = discover_version_golden(
            inferred_root, pred.skill_id, pred.version, pred.task_class
        )
        if versioned is not None:
            _add(versioned)
        shared = discover_golden(inferred_root, pred.skill_id, pred.task_class)
        if shared is not None:
            _add(shared)
    return dirs


def _passed_dirs_from_log(
    golden_set_ref: str | None,
    *,
    log_dir: Path,
    repo_root: Path | None,
    skill_id: str,
    version: int,
) -> list[Path]:
    candidates: list[Path] = [log_dir / f"{skill_id}-v{version}.json"]
    if golden_set_ref:
        ref = Path(golden_set_ref)
        candidates.append(ref)
        if repo_root is not None:
            candidates.append(repo_root / golden_set_ref)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dirs: list[Path] = []
        for row in payload.get("results", []):
            if not row.get("passed"):
                continue
            raw = row.get("golden_path")
            if not raw:
                continue
            dirs.append(Path(raw))
        return dirs
    return []


def _predecessor_regressions(predecessor_dirs: list[Path], report: Any) -> list[str]:
    """Fixture names the predecessor passed that the candidate did not."""

    by_path: dict[str, Any] = {}
    for result in report.results:
        if not result.golden_path:
            continue
        try:
            by_path[str(Path(result.golden_path).resolve())] = result
        except OSError:
            by_path[result.golden_path] = result
    failing: list[str] = []
    for pred_dir in predecessor_dirs:
        try:
            key = str(pred_dir.resolve()) if pred_dir.exists() else str(pred_dir)
        except OSError:
            key = str(pred_dir)
        matched = by_path.get(key)
        if matched is None or not matched.passed:
            failing.append(pred_dir.name or str(pred_dir))
    return failing


def _run_regression(
    ver: SkillVersion,
    *,
    golden_dir: Path | None,
    golden_root: Path | None,
    runs_root: Path,
    require_task_class_gate: bool,
    extra_golden_dirs: list[Path],
    factory: object | None = None,
    executor: object | None = None,
    gate: GateRunner | None = None,
) -> Any:
    from recertia.review.gate import GoldenGateRunner, GraphOrchestratorFactory

    if gate is not None:
        runner = gate
    elif factory is not None:
        runner = GoldenGateRunner(factory)  # type: ignore[arg-type]
    elif executor is not None:
        runner = GoldenGateRunner(GraphOrchestratorFactory(executor))  # type: ignore[arg-type]
    else:
        runner = GoldenGateRunner()
    try:
        return runner.run(
            ver,
            runs_root=runs_root,
            golden_root=golden_root,
            golden_dir=golden_dir,
            require_task_class_gate=require_task_class_gate,
            require_fixture=True,
            extra_golden_dirs=extra_golden_dirs,
        )
    except ValueError as exc:
        raise PromotionError(str(exc)) from exc


def _portable_ref(log_path: Path, repo_root: Path | None) -> str:
    resolved = log_path.resolve()
    if repo_root is not None:
        try:
            return resolved.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            pass
    parts = resolved.parts
    if "evals" in parts:
        idx = parts.index("evals")
        return "/".join(parts[idx:])
    return str(resolved)


def _report_json(report: Any) -> str:
    results: list[dict] = []
    for result in report.results:
        row = asdict(result)
        row["at"] = result.at.isoformat()
        results.append(row)
    payload = {"all_passed": report.all_passed, "results": results}
    return json.dumps(payload, indent=2) + "\n"


def _result_json(result: Any) -> str:
    """Backward-compatible helper for callers that still log a single result."""

    class _One:
        def __init__(self, item: Any) -> None:
            self.results = [item]
            self.all_passed = bool(getattr(item, "passed", False))

    return _report_json(_One(result))
