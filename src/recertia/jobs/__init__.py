"""M7 improvement-plane jobs: proposals only — never write approved directly."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from contracts.policy import JOB_PRIORITY_ORDER, JobPriority, JobQuota, Policy
from contracts.skill import SkillVersion
from recertia.memory.procedural.store import SkillStore
from recertia.review import ReviewService

if TYPE_CHECKING:
    from contracts.eval import MetricReport

JOB_NAME_TO_PRIORITY: dict[str, JobPriority] = {
    "recertify": "recertifier",
    "recertifier": "recertifier",
    "curator": "curator_retire",
    "curate": "curator_retire",
    "curator_retire": "curator_retire",
    "fail_cluster": "fail_cluster_author",
    "fail_cluster_author": "fail_cluster_author",
    "practice": "practice_band",
    "practice_band": "practice_band",
    "practice_hex": "practice_hex",
    "hex": "practice_hex",
    "compress": "compress",
    # mine is offline bootstrap; share curator_retire budget class (low volume).
    "mine": "curator_retire",
    "miner": "curator_retire",
}


def resolve_job_priority(job_name: str) -> JobPriority | None:
    if job_name in JOB_PRIORITY_ORDER:
        return job_name  # type: ignore[return-value]
    return JOB_NAME_TO_PRIORITY.get(job_name)


class JobError(Exception):
    """Job failed or attempted a forbidden write."""


ProposalKind = Literal[
    "mine",
    "curate",
    "practice",
    "recertify",
    "parallelise",
    "serialise",
    "correction",
    "compress",
    "fail_cluster",
    "hex",
]


@dataclass
class Proposal:
    kind: ProposalKind
    skill_id: str
    version: int
    rationale: str
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class JobBudget:
    max_proposals: int = 10
    max_cost_usd: float = 1.0
    max_tokens: int = 0


@dataclass
class JobResult:
    job: str
    proposals: list[Proposal]
    spent_usd: float = 0.0
    skipped: str | None = None


class JobRunner:
    """Runs offline jobs under a budget; write path is proposals → review/golden only."""

    def __init__(
        self,
        store: SkillStore,
        *,
        reviewer: ReviewService | None = None,
        golden_root: Path | None = None,
        runs_root: Path | None = None,
        quota: JobQuota | None = None,
        quota_path: Path | str | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.store = store
        self.reviewer = reviewer
        self.golden_root = golden_root
        self.runs_root = Path(runs_root) if runs_root is not None else Path("/tmp/recertia-jobs")
        self.quota_path = Path(quota_path) if quota_path is not None else None
        self.policy = policy
        self.enablement_report: MetricReport | None = None
        self.hex_recovery = False
        if quota is not None:
            self.quota = quota
        else:
            self.quota = JobQuota()

    def admit(self, job: JobPriority, *, task_class: str | None = None, tokens: int = 0) -> bool:
        return self.quota.can_admit(job, task_class=task_class, tokens=tokens)

    def run(
        self,
        job_name: str,
        fn: Callable[[], list[Proposal]],
        *,
        budget: JobBudget,
        task_class: str | None = None,
    ) -> JobResult:
        priority = resolve_job_priority(job_name)
        tokens = budget.max_tokens
        if priority in {"practice_hex", "compress"}:
            from recertia.jobs.enablement import hex_compress_skip_reason

            skip = hex_compress_skip_reason(
                self.policy,
                self.enablement_report,
                job=priority,
                recovery=self.hex_recovery,
            )
            if skip:
                return JobResult(job=job_name, proposals=[], skipped=skip)
        if priority is not None and not self.admit(
            priority, tokens=tokens, task_class=task_class
        ):
            return JobResult(job=job_name, proposals=[], skipped=f"quota refused {job_name}")
        proposals = fn()
        if len(proposals) > budget.max_proposals:
            raise JobError(f"job {job_name} exceeded max_proposals={budget.max_proposals}")
        if priority is not None and tokens:
            self.quota = self.quota.charge(priority, tokens, task_class=task_class)
            self._persist_quota()
        return JobResult(job=job_name, proposals=proposals[: budget.max_proposals])

    def _persist_quota(self) -> None:
        if self.quota_path is None:
            return
        from recertia.policy_load import QuotaSidecar

        QuotaSidecar(self.quota_path).save(self.quota)

    def submit_proposal(self, proposal: Proposal, draft: SkillVersion) -> str:
        """Persist a candidate draft only — jobs never write ``approved`` (M7).

        Promotion remains an external golden-gate step outside the job plane.
        """

        self.store.write_candidate(draft)
        if self.reviewer is not None:
            decision = self.reviewer.decide(draft, run_id=f"job-{proposal.kind}")
            if decision.outcome != "approved":
                return f"rejected:{decision.note}"
            return f"candidate:{draft.skill_id}@v{draft.version}:review-ok"
        return f"candidate:{draft.skill_id}@v{draft.version}"


def build_job_runner(
    store: SkillStore,
    *,
    runs_root: Path | str,
    policy: Policy | None = None,
    reviewer: ReviewService | None = None,
    golden_root: Path | None = None,
) -> JobRunner:
    """Operator constructor: policy caps + sidecar spend."""

    from recertia.policy_load import QuotaSidecar, load_policy

    runs = Path(runs_root)
    loaded = policy or load_policy()
    sidecar = runs / "job_quota.json"
    quota = QuotaSidecar(sidecar).merge(loaded.job_quota)
    return JobRunner(
        store,
        reviewer=reviewer,
        golden_root=golden_root,
        runs_root=runs,
        quota=quota,
        quota_path=sidecar,
        policy=loaded,
    )
