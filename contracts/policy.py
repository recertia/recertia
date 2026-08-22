"""Versioned T2 policy documents (specs §13.5, §25; references §1.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobPriority = Literal[
    "recertifier",
    "curator_retire",
    "fail_cluster_author",
    "practice_band",
    "practice_hex",
    "compress",
]

JOB_PRIORITY_ORDER: tuple[JobPriority, ...] = (
    "recertifier",
    "curator_retire",
    "fail_cluster_author",
    "practice_band",
    "practice_hex",
    "compress",
)

COMPUTER_USE_TASK_CLASSES: tuple[str, ...] = (
    "bug-reproduction",
    "playtest-operator",
    "docs-auditor",
)


class AuthoringPrior(BaseModel):
    """Rules the distiller must apply on every success or failure-cluster path."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    version: str = Field(min_length=1)
    require_parameter_or_recurrence: bool = True
    require_non_judge_criterion: bool = True
    require_sensitivity_proof: bool = True
    require_failure_modes: bool = True
    max_steps: int = Field(default=12, ge=1, le=50)
    prefer_shell_when_applicable: bool = True
    notes: list[str] = Field(default_factory=list)


class PolicyBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_max_attempts: int = Field(default=3, ge=1)
    default_max_cost_usd: float = Field(default=1.0, gt=0)
    ablation_rate: float = Field(default=0.1, ge=0.0, le=1.0)


class ImprovementFlags(BaseModel):
    """T2 algorithm flags. These MUST NOT gate graph topology (ADR-0015)."""

    model_config = ConfigDict(extra="forbid")

    miner_lint: bool = True
    fail_cluster_curriculum: bool = True
    provenance_diversity_gate: bool = True
    deterministic_guide: bool = False
    curator_compress: bool = False
    compose_chain_review: bool = True
    practice_hex_search: bool = False
    external_trajectory_import: bool = True
    long_lived_computer_backend: bool = False


class ImprovementLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_cluster_min_runs: int = Field(default=3, ge=1)
    fail_cluster_min_sessions: int = Field(default=2, ge=1)
    min_source_sessions: int = Field(default=2, ge=1)
    compress_max_units: int = Field(default=12, ge=1, le=12)
    compress_delta_hard: float = Field(default=0.0, ge=0.0)
    compress_delta_cell: float = Field(default=0.02, ge=0.0)
    compress_rho: float = Field(default=0.25, ge=0.0, le=1.0)
    practice_hex_probe_budget: int = Field(default=8, ge=1)
    practice_hex_patch_budget: int = Field(default=2, ge=1)
    practice_hex_rounds: int = Field(default=2, ge=1)


class JobQuota(BaseModel):
    """Weekly improvement-plane token QoS (ADR-0015). HEX/compress are leftover-only."""

    model_config = ConfigDict(extra="forbid")

    weekly_token_cap: int = Field(default=500_000, ge=0)
    hex_share: float = Field(default=0.25, ge=0.0, le=1.0)
    max_hex_jobs_per_task_class: int = Field(default=1, ge=0)
    max_status_writes_per_tick: int = Field(default=50, ge=1)
    max_compress_candidates_per_tick: int = Field(default=1, ge=0)
    computer_use_practice_share: float = Field(default=0.15, ge=0.0, le=1.0)
    tokens_spent: int = Field(default=0, ge=0)
    hex_tokens_spent: int = Field(default=0, ge=0)
    computer_use_tokens_spent: int = Field(default=0, ge=0)
    hex_jobs_by_class: dict[str, int] = Field(default_factory=dict)

    def remaining(self) -> int:
        return max(0, self.weekly_token_cap - self.tokens_spent)

    def hex_remaining(self) -> int:
        cap = int(self.weekly_token_cap * self.hex_share)
        leftover = self.remaining()
        return max(0, min(cap - self.hex_tokens_spent, leftover))

    def computer_use_remaining(self) -> int:
        cap = int(self.weekly_token_cap * self.computer_use_practice_share)
        leftover = self.remaining()
        return max(0, min(cap - self.computer_use_tokens_spent, leftover))

    @staticmethod
    def _computer_use_class(task_class: str | None) -> bool:
        return task_class in COMPUTER_USE_TASK_CLASSES

    def can_admit(self, job: JobPriority, *, task_class: str | None = None, tokens: int = 0) -> bool:
        if self._computer_use_class(task_class) and job in (
            "practice_band",
            "practice_hex",
        ):
            remaining = self.computer_use_remaining()
            if remaining <= 0 or remaining < tokens:
                return False
        if job in ("recertifier", "curator_retire", "fail_cluster_author", "practice_band"):
            return self.remaining() >= tokens
        if job == "practice_hex":
            if task_class is not None:
                used = self.hex_jobs_by_class.get(task_class, 0)
                if used >= self.max_hex_jobs_per_task_class:
                    return False
            return self.hex_remaining() >= tokens
        if job == "compress":
            return self.remaining() >= tokens
        return False

    def charge(self, job: JobPriority, tokens: int, *, task_class: str | None = None) -> "JobQuota":
        hex_spent = self.hex_tokens_spent + tokens if job == "practice_hex" else self.hex_tokens_spent
        cu_spent = self.computer_use_tokens_spent
        if self._computer_use_class(task_class) and job in ("practice_band", "practice_hex"):
            cu_spent += tokens
        by_class = dict(self.hex_jobs_by_class)
        if job == "practice_hex" and task_class is not None:
            by_class[task_class] = by_class.get(task_class, 0) + 1
        return self.model_copy(
            update={
                "tokens_spent": self.tokens_spent + tokens,
                "hex_tokens_spent": hex_spent,
                "computer_use_tokens_spent": cu_spent,
                "hex_jobs_by_class": by_class,
            }
        )


class StateManagement(BaseModel):
    """Working-set residency and read-only caches (ADR-0018). T2; default offload is off."""

    model_config = ConfigDict(extra="forbid")

    idle_offload_enabled: bool = False
    quiet_threshold_s: float = Field(default=60.0, ge=0.0)
    eligible_surfaces: list[str] = Field(
        default_factory=lambda: ["workspace", "checkpoint", "retrieval_index"]
    )
    restore_latency_budget_frac: float = Field(default=0.05, ge=0.0, le=1.0)
    tool_result_cache_enabled: bool = True
    tool_result_cache_ttl_s: float = Field(default=120.0, ge=0.0)
    retrieval_cache_enabled: bool = True
    retrieval_cache_ttl_s: float = Field(default=30.0, ge=0.0)


class IsolationSettings(BaseModel):
    """Default execution isolation. External computer is opt-in only (ADR-0019)."""

    model_config = ConfigDict(extra="forbid")

    default_backend: Literal["container", "local"] = "container"
    allow_external_computer: bool = False
    external_computer_ttl_seconds: int = Field(default=3600, ge=1)
    external_computer_allowlist: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    """Versioned T2 policy document: thresholds, budgets, and authoring prior pointer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    version: str = Field(min_length=1)
    authoring_prior_version: str = Field(min_length=1)
    budgets: PolicyBudgets = Field(default_factory=PolicyBudgets)
    shadow_min_lift: float = Field(default=0.05, ge=0.0)
    evidence_floor: int = Field(default=30, ge=1)
    active_cap_per_task_class: int = Field(default=50, ge=1)
    require_tool_approval_for_non_read: bool = True
    min_independent_runs: int = Field(default=5, ge=1)
    faithfulness_interventions_enabled: bool = False
    improvement: ImprovementFlags = Field(default_factory=ImprovementFlags)
    improvement_limits: ImprovementLimits = Field(default_factory=ImprovementLimits)
    job_quota: JobQuota = Field(default_factory=JobQuota)
    state_management: StateManagement = Field(default_factory=StateManagement)
    isolation: IsolationSettings = Field(default_factory=IsolationSettings)
    notes: list[str] = Field(default_factory=list)
