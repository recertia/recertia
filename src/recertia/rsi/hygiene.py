"""HygieneGate. Fresh execution plus leakage screen. Spec R1, R13, R14.

Does not write SkillVersion or the retrieval index.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from recertia.rsi.leakage import SCAN_VERSION, screen_leakage
from recertia.rsi.models import (
    PAYLOAD_CAP_BYTES,
    ExecResult,
    HygieneRequest,
    HygieneResult,
    NegativeCase,
    ScreenedArtifact,
)
from recertia.rsi.world import World

Executor = Callable[[dict], ExecResult]


class _Flight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[HygieneResult] = None


class HygieneGate:
    def __init__(self, world: World, executor: Executor) -> None:
        self.world = world
        self.executor = executor
        self._guard = threading.Lock()
        self._inflight: dict[tuple, _Flight] = {}

    def __call__(self, req: HygieneRequest) -> HygieneResult:
        return self.run(req)

    def run(self, req: HygieneRequest) -> HygieneResult:
        cached = self.world.idempotency.get(req.idempotency_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        parent, import_hash = self._identity(req)
        lock_key = (parent or import_hash, req.criteria_hash)
        existing = self.world.accepted_keys.get(lock_key)
        if existing is not None:
            art = self.world.screened[existing]
            result = HygieneResult(
                accepted=False,
                reason="duplicate",
                ledger_receipt=art.ledger_receipt,
                checks_passed=art.checks_passed,
                checks_total=art.checks_total,
                strict_success=art.strict_success,
                artifact_id=None,
                fresh_execution_id=art.fresh_execution_id,
                duplicate_of=art.artifact_id,
            )
            self._remember(req.idempotency_key, result)
            return result

        leader, flight = self._begin(lock_key)
        if not leader:
            assert flight is not None
            flight.event.wait()
            assert flight.result is not None
            result = flight.result
            if result.reason == "ok" and result.artifact_id:
                dup = HygieneResult(
                    accepted=False,
                    reason="duplicate",
                    ledger_receipt=result.ledger_receipt,
                    checks_passed=result.checks_passed,
                    checks_total=result.checks_total,
                    strict_success=result.strict_success,
                    duplicate_of=result.artifact_id,
                    fresh_execution_id=result.fresh_execution_id,
                )
                self._remember(req.idempotency_key, dup)
                return dup
            self._remember(req.idempotency_key, result)
            return result

        try:
            result = self._execute(req, parent, import_hash)
            flight.result = result
            return result
        finally:
            with self._guard:
                self._inflight.pop(lock_key, None)
            flight.event.set()

    def _begin(self, lock_key: tuple) -> tuple[bool, _Flight]:
        with self._guard:
            flight = self._inflight.get(lock_key)
            if flight is None:
                flight = _Flight()
                self._inflight[lock_key] = flight
                return True, flight
            return False, flight

    def _identity(self, req: HygieneRequest) -> tuple[Optional[str], Optional[str]]:
        if req.trajectory_id is not None:
            return req.trajectory_id, None
        raw = json.dumps(req.import_payload, sort_keys=True, separators=(",", ":"))
        return None, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _execute(
        self,
        req: HygieneRequest,
        parent: Optional[str],
        import_hash: Optional[str],
    ) -> HygieneResult:
        crit = self.world.criteria.get(req.criteria_hash)
        if crit is None or crit.task_class != req.task_class:
            return self._reject(req, "criteria_mismatch", parent, import_hash, sha="", executed=None)

        if req.sandbox_policy_id not in self.world.allow_policies:
            return self._reject(req, "sandbox_error", parent, import_hash, sha="", executed=None)

        public = self._public_view(req)
        self.world.sandbox_calls += 1
        executed = self.executor(public)
        self.world.exec_seconds.append(executed.elapsed_s)

        if executed.error == "timeout":
            return self._reject(req, "timeout", parent, import_hash, sha="", executed=executed)
        if executed.error == "sandbox_error":
            return self._reject(req, "sandbox_error", parent, import_hash, sha="", executed=executed)

        findings = screen_leakage(executed.actions, executed.observations, SCAN_VERSION)
        payload = {
            "actions": executed.actions,
            "observations": executed.observations,
            "verifier": {
                "checks_passed": executed.checks_passed,
                "checks_total": executed.checks_total,
                "strict_success": executed.strict_success,
            },
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if findings:
            return self._reject(req, "leakage", parent, import_hash, sha=digest, executed=executed, raw=None)
        if len(raw) > PAYLOAD_CAP_BYTES:
            return self._reject(
                req, "payload_too_large", parent, import_hash, sha=digest, executed=executed, raw=None
            )
        if not executed.strict_success and executed.checks_passed < 0:
            return self._reject(req, "verifier_fail", parent, import_hash, sha=digest, executed=executed)

        # Verifier failure is an outcome, not an exception. Partial progress is not acceptance
        # unless the fresh run reports strict success. Spec: V(x)=1 equivalent is strict_success.
        if not executed.strict_success:
            return self._reject(req, "verifier_fail", parent, import_hash, sha=digest, executed=executed)

        compressed = gzip.compress(raw)
        uri = f"blob:{digest}"
        self.world.blobs[uri] = compressed
        now = _now()
        artifact_id = "scr_" + digest[:16]
        receipt_body = {
            "artifact_id": artifact_id,
            "payload_sha256": digest,
            "payload_uri": uri,
            "payload_bytes": len(raw),
            "criteria_hash": req.criteria_hash,
            "parent_trajectory_id": parent,
            "import_hash": import_hash,
            "fresh_execution_id": executed.fresh_execution_id,
            "strict_success": True,
            "checks_passed": executed.checks_passed,
            "checks_total": executed.checks_total,
        }
        receipt = self.world.ledger.append("screened_artifact", receipt_body)
        artifact = ScreenedArtifact(
            artifact_id=artifact_id,
            criteria_hash=req.criteria_hash,
            task_class=req.task_class,
            fresh_execution_id=executed.fresh_execution_id,
            payload_uri=uri,
            payload_sha256=digest,
            payload_bytes=len(raw),
            checks_passed=executed.checks_passed,
            checks_total=executed.checks_total,
            strict_success=True,
            leakage_scan_version=SCAN_VERSION,
            producing_job_id=self.world.job_id,
            ledger_receipt=receipt,
            created_at=now,
            parent_trajectory_id=parent,
            import_hash=import_hash,
        )
        self.world.screened[artifact_id] = artifact
        self.world.accepted_keys[(parent or import_hash, req.criteria_hash)] = artifact_id
        self.world.ledger.append(
            "stage",
            {
                "snapshot_id": artifact_id,
                "stage": "hygiene",
                "retained": True,
                "metrics": {"strict_count": 1.0, "process_score": 1.0, "failure_share": 0.0},
                "parent_receipt": receipt,
                "index_generation": 0,
                "active_skill_ids": [],
            },
        )
        result = HygieneResult(
            accepted=True,
            reason="ok",
            ledger_receipt=receipt,
            checks_passed=executed.checks_passed,
            checks_total=executed.checks_total,
            strict_success=True,
            artifact_id=artifact_id,
            fresh_execution_id=executed.fresh_execution_id,
        )
        self._remember(req.idempotency_key, result)
        self._count(result.reason)
        return result

    def _reject(
        self,
        req: HygieneRequest,
        reason: str,
        parent: Optional[str],
        import_hash: Optional[str],
        sha: str,
        executed: Optional[ExecResult],
        raw: Optional[bytes] = None,
    ) -> HygieneResult:
        del raw  # never persisted on the reject path
        now = _now()
        raw_id = f"{reason}|{parent}|{import_hash}|{sha}|{now}"
        case_id = "neg_" + hashlib.sha256(raw_id.encode()).hexdigest()[:16]
        body = {
            "case_id": case_id,
            "reason": reason,
            "criteria_hash": req.criteria_hash,
            "payload_sha256": sha,
            "stores_body": False,
            "parent_trajectory_id": parent,
            "import_hash": import_hash,
        }
        receipt = self.world.ledger.append("negative_case", body)
        self.world.negatives[case_id] = NegativeCase(
            case_id=case_id,
            reason=reason,
            criteria_hash=req.criteria_hash,
            payload_sha256=sha,
            ledger_receipt=receipt,
            created_at=now,
            stores_body=False,
            parent_trajectory_id=parent,
            import_hash=import_hash,
        )
        result = HygieneResult(
            accepted=False,
            reason=reason,
            ledger_receipt=receipt,
            checks_passed=0 if executed is None else executed.checks_passed,
            checks_total=0 if executed is None else executed.checks_total,
            strict_success=False,
            fresh_execution_id=None if executed is None else executed.fresh_execution_id,
        )
        self._remember(req.idempotency_key, result)
        self._count(reason)
        return result

    def _public_view(self, req: HygieneRequest) -> dict:
        if req.trajectory_id is not None:
            traj = self.world.trajectories.get(req.trajectory_id)
            if traj is None:
                return {"public": {}, "criteria_hash": req.criteria_hash, "task_class": req.task_class}
            # Private bag is not forwarded. Spec: fresh execution sees the public prefix only.
            return {
                "public": dict(traj.public),
                "criteria_hash": req.criteria_hash,
                "task_class": req.task_class,
                "trajectory_id": traj.trajectory_id,
            }
        return {
            "public": dict(req.import_payload or {}),
            "criteria_hash": req.criteria_hash,
            "task_class": req.task_class,
        }

    def _remember(self, key: str, result: HygieneResult) -> None:
        self.world.idempotency[key] = result

    def _count(self, reason: str) -> None:
        self.world.reject_counts[reason] = self.world.reject_counts.get(reason, 0) + 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reject_rate(world: World, reason: str) -> float:
    total = sum(world.reject_counts.values())
    if total == 0:
        return 0.0
    return world.reject_counts.get(reason, 0) / total
