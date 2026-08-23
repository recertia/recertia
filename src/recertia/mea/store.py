"""Sidecar persistence for AuditedTaskState (not on RunState).

Keeps run/branch JSON Schema free of the MEA projection while remaining
resumable from the same runs_root as checkpoints. The directory is created
lazily on first save so the default single-request path writes nothing.
"""

from __future__ import annotations

from pathlib import Path

from contracts.audited_task_state import AuditedTaskState


class AuditedStateStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, run_id: str) -> Path:
        safe = run_id.replace("/", "_").replace("..", "_")
        return self.root / f"{safe}.json"

    def load(self, run_id: str) -> AuditedTaskState | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        return AuditedTaskState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, run_id: str, state: AuditedTaskState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path_for(run_id).write_text(state.model_dump_json(indent=2), encoding="utf-8")
