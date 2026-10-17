"""Golden-suite runs for ``POST /v1/evals/runs``."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class EvalService:
    """Runs one golden suite and returns the HTTP result body."""

    def run_suite(
        self,
        *,
        task_class: str,
        golden_root: Path,
        skills_root: Path,
        runs_root: Path,
        eval_db: Path,
        snapshot_id: str,
        golden_dir: Path | None,
    ) -> dict[str, Any]:
        from recertia.evals.golden import run_eval_suite
        from recertia.evals.store import EvalStore
        from recertia.review.gate import default_golden_factory

        store = EvalStore(eval_db)
        try:
            report = run_eval_suite(
                task_class=task_class,
                golden_root=golden_root,
                skills_root=skills_root,
                runs_root=runs_root,
                eval_store=store,
                snapshot_id=snapshot_id,
                golden_dir=golden_dir,
                factory=default_golden_factory(),
            )
        finally:
            store.close()
        return {
            "all_passed": report.all_passed,
            "results": [
                {
                    "skill_id": row.skill_id,
                    "passed": row.passed,
                    "terminal": row.terminal,
                    "run_id": row.run_id,
                    "detail": row.detail,
                }
                for row in report.results
            ],
        }
