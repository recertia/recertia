"""Review service: queue, decisions, shared golden gate (specs §4, §8)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from contracts.review import ReviewDecision
from contracts.skill import SkillVersion
from recertia.memory.procedural.hygiene import scan_skill
from recertia.ports.orchestration import GateRunner

DecisionPolicy = Callable[[SkillVersion, str], Literal["approved", "rejected"]]


class ReviewError(Exception):
    """Draft cannot be approved."""


class ReviewService:
    """Filesystem-backed review queue. Approval always shares M1's golden runner."""

    def __init__(
        self,
        root: Path | str,
        *,
        golden_root: Path | None = None,
        runs_root: Path | None = None,
        policy: DecisionPolicy | None = None,
        gate: GateRunner | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.root / "queue.jsonl"
        self.decisions_dir = self.root / "decisions"
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        self.golden_root = golden_root
        self.runs_root = runs_root or (self.root / "review-runs")
        self.policy = policy or self._auto_approve_if_clean
        self.gate = gate

    def enqueue(self, version: SkillVersion, *, run_id: str) -> str:
        item_id = f"rev-{uuid.uuid4().hex[:10]}"
        with self.queue_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "item_id": item_id,
                        "skill_id": version.skill_id,
                        "version": version.version,
                        "run_id": run_id,
                        "enqueued_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                + "\n"
            )
        return item_id

    def decide(
        self,
        version: SkillVersion,
        *,
        run_id: str,
        reviewer: str = "policy",
    ) -> ReviewDecision:
        hygiene = scan_skill(version)
        if hygiene.secret_scan == "failed":
            return self._write_decision(
                version,
                run_id=run_id,
                outcome="rejected",
                reviewer=reviewer,
                note="hygiene scan failed; draft contains a secret pattern",
            )

        # A review decision may not approve when no applicable golden suite is
        # configured.  This is deliberately fail-closed: policy is advisory
        # without independently executable regression evidence.
        if self.golden_root is None:
            return self._write_decision(
                version,
                run_id=run_id,
                outcome="rejected",
                reviewer=reviewer,
                note="golden regression gate is required for approval",
            )
        report = self._run_gate(version)
        golden_ref = str(
            self.decisions_dir / f"{version.skill_id}-v{version.version}-golden.json"
        )
        report.write(Path(golden_ref))
        if not report.results or not report.all_passed:
            return self._write_decision(
                version,
                run_id=run_id,
                outcome="rejected",
                reviewer=reviewer,
                note="golden regression gate failed or had no applicable fixture",
                golden_report_ref=golden_ref,
            )

        outcome = self.policy(version, run_id)
        return self._write_decision(
            version,
            run_id=run_id,
            outcome=outcome,
            reviewer=reviewer,
            note="policy decision after hygiene" + (" and golden gate" if golden_ref else ""),
            golden_report_ref=golden_ref,
        )

    def _run_gate(self, version: SkillVersion) -> Any:
        gate = self.gate
        if gate is None:
            from recertia.review.gate import GoldenGateRunner

            gate = GoldenGateRunner()
        assert self.golden_root is not None
        return gate.run(
            version,
            golden_root=self.golden_root,
            runs_root=self.runs_root,
        )

    def _write_decision(
        self,
        version: SkillVersion,
        *,
        run_id: str,
        outcome: Literal["approved", "rejected", "changes_requested"],
        reviewer: str,
        note: str | None,
        golden_report_ref: str | None = None,
    ) -> ReviewDecision:
        decision = ReviewDecision(
            decision_id=f"dec-{uuid.uuid4().hex[:10]}",
            skill_id=version.skill_id,
            version=version.version,
            run_id=run_id,
            outcome=outcome,
            reviewer=reviewer,
            note=note,
            golden_report_ref=golden_report_ref,
            decided_at=datetime.now(timezone.utc),
            policy="m3-review-service",
        )
        path = self.decisions_dir / f"{decision.decision_id}.json"
        path.write_text(decision.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return decision

    @staticmethod
    def _auto_approve_if_clean(
        version: SkillVersion, run_id: str
    ) -> Literal["approved", "rejected"]:
        proven = any(
            c.kind != "judge" and c.is_required and c.is_preregistered_and_proven
            for c in version.certification_criteria
        )
        return "approved" if proven else "rejected"
