"""Append-only trajectory store: one jsonl per run under ``{runs_root}/trajectories/``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from contracts.trajectory import Trajectory, TrajectoryEvent


class TrajectoryStore:
    """Persist trajectory events. Failures here must never fail a production run."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        safe = run_id.replace("/", "_").replace("..", "_")
        return self.root / f"{safe}.jsonl"

    def _meta_path(self, run_id: str) -> Path:
        safe = run_id.replace("/", "_").replace("..", "_")
        return self.root / f"{safe}.meta.json"

    def write_meta(
        self,
        *,
        run_id: str,
        task_id: str,
        task_class: str | None = None,
        arm: str = "treatment",
        is_eval_fixture: bool = False,
    ) -> None:
        meta = {
            "run_id": run_id,
            "task_id": task_id,
            "task_class": task_class,
            "arm": arm,
            "is_eval_fixture": is_eval_fixture,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        self._meta_path(run_id).write_text(json.dumps(meta, indent=2) + "\n")

    def next_seq(self, run_id: str) -> int:
        path = self._path(run_id)
        if not path.exists():
            return 0
        n = 0
        with path.open() as f:
            for _ in f:
                n += 1
        return n

    def append_assigned(self, run_id: str, events: list[TrajectoryEvent]) -> None:
        """Write events that already have ``seq`` assigned (SQLite is the source)."""

        if not events:
            return
        path = self._path(run_id)
        with path.open("a") as handle:
            for event in events:
                handle.write(event.model_dump_json() + "\n")

    def append_many(self, run_id: str, events: list[TrajectoryEvent]) -> list[TrajectoryEvent]:
        if not events:
            return []
        seq = self.next_seq(run_id)
        written: list[TrajectoryEvent] = []
        path = self._path(run_id)
        with path.open("a") as f:
            for ev in events:
                assigned = ev.model_copy(update={"seq": seq, "run_id": run_id})
                f.write(assigned.model_dump_json() + "\n")
                written.append(assigned)
                seq += 1
        return written

    def list_events(self, run_id: str) -> list[TrajectoryEvent]:
        path = self._path(run_id)
        if not path.exists():
            return []
        out: list[TrajectoryEvent] = []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                out.append(TrajectoryEvent.model_validate_json(line))
        return out

    def get_trajectory(self, run_id: str) -> Trajectory | None:
        events = self.list_events(run_id)
        meta_path = self._meta_path(run_id)
        if not events and not meta_path.exists():
            return None
        task_id = run_id
        task_class = None
        arm = "treatment"
        is_eval = False
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            task_id = meta.get("task_id", run_id)
            task_class = meta.get("task_class")
            arm = meta.get("arm", "treatment")
            is_eval = bool(meta.get("is_eval_fixture", False))
        closed = any(e.event_kind == "terminal" for e in events)
        closed_at = None
        if closed:
            for e in reversed(events):
                if e.event_kind == "terminal":
                    closed_at = e.at
                    break
        return Trajectory(
            run_id=run_id,
            task_id=task_id,
            task_class=task_class,
            arm=arm,  # type: ignore[arg-type]
            is_eval_fixture=is_eval,
            events=events,
            closed=closed,
            closed_at=closed_at,
        )

    def iter_run_ids(self) -> list[str]:
        ids: list[str] = []
        for p in self.root.glob("*.jsonl"):
            ids.append(p.stem)
        return sorted(ids)

    def iter_trajectories(self) -> list[Trajectory]:
        out: list[Trajectory] = []
        for run_id in self.iter_run_ids():
            traj = self.get_trajectory(run_id)
            if traj is not None:
                out.append(traj)
        return out
