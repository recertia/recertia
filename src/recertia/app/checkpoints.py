"""Read checkpoints and audited task state. No PDP; callers already authorized."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from contracts.audited_task_state import AuditedTaskState
from recertia.graph.persistence import open_run_persistence
from recertia.ops.mea_systems import build_mea_systems_brief
from recertia.ports.identity import TenantId


class CheckpointReader:
    """Latest checkpoint, history, and AuditedTaskState. Does not write.

    The API reads ``<root>/runs/<tenant>`` and passes ``runtime_root=<root>``, so
    the handle takes ``<root>/.lock`` shared, like workers, backup and restore, and
    then its tenant lock ``<root>/runs/<tenant>/.tenant.lock`` shared. Without it the
    handle locks ``<root>/runs/<tenant>/.lock`` instead, which backup and restore do
    not take, so they do not exclude it. The tenant lock is the same file either way,
    so a leftover import it runs still excludes a worker's import of the same tenant
    (REC-065).
    """

    def latest(
        self,
        tenant: TenantId | str,
        run_id: str,
        *,
        runs_root: Path,
        runtime_root: Path | None = None,
    ) -> Any | None:
        del tenant
        persistence = open_run_persistence(runs_root, runtime_root=runtime_root)
        try:
            row = persistence.checkpoints.latest(run_id)
            if row is None:
                return None
            return row[3]
        finally:
            persistence.close()

    def latest_row(
        self,
        tenant: TenantId | str,
        run_id: str,
        *,
        runs_root: Path,
        runtime_root: Path | None = None,
    ) -> tuple[int, str, str | None, Any] | None:
        del tenant
        persistence = open_run_persistence(runs_root, runtime_root=runtime_root)
        try:
            return persistence.checkpoints.latest(run_id)
        finally:
            persistence.close()

    def history(
        self,
        tenant: TenantId | str,
        run_id: str,
        *,
        runs_root: Path,
        runtime_root: Path | None = None,
    ) -> list[tuple[int, str, str | None]]:
        del tenant
        persistence = open_run_persistence(runs_root, runtime_root=runtime_root)
        try:
            rows = persistence.checkpoints.history(run_id)
            return [(seq, node, nxt) for seq, node, nxt, _state in rows]
        finally:
            persistence.close()

    def audited_state(
        self,
        tenant: TenantId | str,
        run_id: str,
        *,
        runs_root: Path,
        runtime_root: Path | None = None,
    ) -> Mapping[str, object] | None:
        del tenant
        persistence = open_run_persistence(runs_root, runtime_root=runtime_root)
        try:
            state = persistence.audited_states.load(run_id)
        finally:
            persistence.close()
        if state is None:
            return None
        return state.model_dump(mode="json")

    def load_audited_file(self, path: Path) -> AuditedTaskState:
        raw = path.read_text(encoding="utf-8")
        import json

        return AuditedTaskState.model_validate(json.loads(raw))

    def verify_audited(self, state: AuditedTaskState) -> list[str]:
        errors: list[str] = []
        if not state.criteria_snapshot_hash:
            errors.append("missing criteria_snapshot_hash")
        if not state.isolation_policy_ref:
            errors.append("missing isolation_policy_ref")
        if state.rounds_consumed > state.max_rounds:
            errors.append("rounds_consumed exceeds max_rounds")
        for decision in state.verified_decisions:
            if not decision.evidence_refs:
                errors.append(f"verified_decision {decision.decision_id} lacks evidence_refs")
        restored = AuditedTaskState.model_validate(state.model_dump(mode="json"))
        if restored.version != state.version or restored.state_id != state.state_id:
            errors.append("round_trip_mismatch")
        return errors

    def systems_brief(
        self, state: AuditedTaskState, *, previous: AuditedTaskState | None = None
    ) -> Any:
        return build_mea_systems_brief(state, previous=previous)
