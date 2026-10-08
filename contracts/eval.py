"""Eval / measurement contracts (specs §11, §19, §23).

ContributionCell is the paired report shape from ADR-0021 (Proposed).
It does not establish causal_lift and it does not retire a skill.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.common import Curation


class ConfidenceInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: float
    high: float
    level: float = Field(default=0.95, ge=0.0, le=1.0)
    method: str = "newcombe_wilson"


class BinomialSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    successes: int = Field(ge=0)
    trials: int = Field(ge=0)

    @property
    def rate(self) -> float | None:
        if self.trials == 0:
            return None
        return self.successes / self.trials


class RunVariance(BaseModel):
    """Best–worst gap and sample std-dev over independent run (or snapshot) rates."""

    model_config = ConfigDict(extra="forbid")

    n_runs: int = Field(ge=0)
    std_dev: float | None = None
    best_rate: float | None = None
    worst_rate: float | None = None
    best_worst_gap: float | None = None


LiftStatus = Literal[
    "established_positive",
    "established_negative",
    "not_established",
    "insufficient_data",
    "low_run_count",
]

Pathway = Literal["applied", "retrieved_unused", "never_retrieved"]
Estimand = Literal["itt", "per_protocol"]
Multiplicity = Literal["primary", "secondary", "exploratory"]
OrderArm = Literal["fixed", "shuffle"]


class ContributionCell(BaseModel):
    """One paired skill-by-stratum contrast. ADR-0021, Proposed.

    Concordant pairs do not contribute. A cell with only judge criteria
    stays null. This model does not write T3 and does not retire a skill.
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str | None = None
    stratum: str
    order_arm: OrderArm
    shuffle_index: int | None = Field(default=None, ge=0)
    n_paired: int = Field(ge=0)
    n_discordant_help: int = Field(ge=0)
    n_discordant_hurt: int = Field(ge=0)
    estimate: float | None = None
    interval: ConfidenceInterval | None = None
    status: LiftStatus
    pathway: Pathway = "never_retrieved"
    estimand: Estimand = "itt"
    multiplicity: Multiplicity = "exploratory"
    holdout: bool = False
    protocol_hash: str | None = None
    attempts_delta: float | None = None
    cost_delta_usd: float | None = None
    null_judge: bool = False

    def refuses_established(self) -> bool:
        if self.holdout or self.null_judge:
            return True
        if self.multiplicity != "primary":
            return True
        if self.estimand != "itt":
            return True
        if self.pathway != "applied":
            return True
        if self.status in {"not_established", "insufficient_data", "low_run_count"}:
            return True
        return False


class CausalLiftResult(BaseModel):
    """Treatment − control first-attempt success with a difference CI (specs §19)."""

    model_config = ConfigDict(extra="forbid")

    task_class: str
    treatment: BinomialSample
    control: BinomialSample
    estimate: float | None
    interval: ConfidenceInterval | None
    status: LiftStatus
    snapshot_id: str | None = None
    model_version: str | None = None
    window: str | None = None
    treatment_variance: RunVariance | None = None
    control_variance: RunVariance | None = None
    lift_variance: RunVariance | None = None
    min_independent_runs: int = Field(default=5, ge=1)
    independent_runs: int = Field(default=0, ge=0)
    protocol_hash: str | None = None
    contribution_cells: list[ContributionCell] = Field(default_factory=list)

    def render_status(self) -> str:
        if self.status == "not_established":
            return "not established"
        if self.status == "insufficient_data":
            return "insufficient data"
        if self.status == "low_run_count":
            return "low run count"
        if self.status == "established_positive":
            return "established positive"
        return "established negative"

    def library_claim_allowed(self) -> bool:
        """A class interval is not a skill result. ADR-0021.

        Secondary cells never authorize the sentence. A fixed-order-only
        gain does not either: a shuffle row must be present and not refuse.
        """

        if self.status != "established_positive":
            return False
        if self.independent_runs < self.min_independent_runs:
            return False
        primary = [
            cell
            for cell in self.contribution_cells
            if cell.multiplicity == "primary" and not cell.holdout
        ]
        if not primary:
            return False
        if any(cell.refuses_established() for cell in primary):
            return False
        if not any(cell.order_arm == "shuffle" for cell in primary):
            return False
        return all(cell.status == "established_positive" for cell in primary)


class ControlBaseline(BaseModel):
    """Persisted per-task-class control-arm baseline (specs §24.2; M5 contribution input)."""

    model_config = ConfigDict(extra="forbid")

    task_class: str
    snapshot_id: str
    model_version: str | None = None
    control: BinomialSample
    interval: ConfidenceInterval | None = None
    created_at: datetime
    report_id: str | None = None


class EvalObservation(BaseModel):
    """One immutable, run-derived observation keyed for aggregation."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_class: str
    arm: Literal["treatment", "control", "shadow", "practice"] = "treatment"
    snapshot_id: str
    model_version: str | None = None
    first_attempt_success: bool
    predicted_success: float | None = None
    terminal: str | None = None
    fixture_id: str | None = None
    is_eval_fixture: bool = False
    recorded_at: datetime
    strategy: str | None = None
    attempt_no: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    abstention_confirmed: bool | None = None
    skill_id: str | None = None
    skill_version: int | None = Field(default=None, ge=1)
    suppressed_skill_id: str | None = None
    suppressed_skill_version: int | None = Field(default=None, ge=1)
    valid_non_judge_evidence: bool = False
    evidence_hash: str | None = None
    curation: Curation | None = None
    practice_converted: bool | None = None


class MetricReport(BaseModel):
    """Aggregate §11 / §23 metrics for one snapshot window."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    model_version: str | None = None
    task_class: str | None = None
    reuse_rate: float | None = None
    first_attempt_success: float | None = None
    attempts_to_success: float | None = None
    cost_per_solved_task: float | None = None
    regression_rate: float | None = None
    causal_lift: CausalLiftResult | None = None
    calibration_error: float | None = None
    abstention_precision: float | None = None
    merge_gap_rate: float | None = None
    parallel_speedup: float | None = None
    fake_edge_rate: float | None = None
    judge_isolation_violations: int = 0
    curation_gap: float | None = None
    practice_conversion: float | None = None
    retirement_reversal_rate: float | None = None
    active_cap_pressure: float | None = None
    judge_false_pass_rate: float | None = None
    mean_composition_depth: float | None = None
    lint_block_rate: float | None = None
    distill_fail_path_share: float | None = None
    practice_hex_accept_rate: float | None = None
    promotion_source_diversity: float | None = None
    lineage_revoke_count: int | None = None
    compress_token_ratio: float | None = None
    compress_perf_delta: float | None = None
    guide_used_rate: float | None = None
    compose_block_rate: float | None = None
    off_intent_activation: float | None = None
    library_yield: float | None = None
    retrieval_precision_at_3: float | None = None
    retrieval_decay: float | None = None
    unavailable: dict[str, str] = Field(default_factory=dict)
    at: datetime
