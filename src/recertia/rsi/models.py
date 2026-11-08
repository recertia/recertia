"""Schemas from spec section 8. Stdlib only."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

PAYLOAD_CAP_BYTES = 8 * 1024 * 1024
LEAKAGE_CODES = (
    "private_guidance",
    "system_prompt_fragment",
    "host_path",
    "secret_env",
)
HYGIENE_REASONS = (
    "ok",
    "verifier_fail",
    "leakage",
    "timeout",
    "sandbox_error",
    "criteria_mismatch",
    "payload_too_large",
    "duplicate",
)
STAGE_NAMES = ("hygiene", "distill", "recalibrate", "promote", "rollback")


@dataclass
class LeakageFinding:
    code: str
    span_hash: str

    def __post_init__(self) -> None:
        if self.code not in LEAKAGE_CODES:
            raise ValueError(f"unknown leakage code: {self.code}")


@dataclass
class HygieneRequest:
    criteria_hash: str
    task_class: str
    sandbox_policy_id: str
    idempotency_key: str
    trajectory_id: Optional[str] = None
    import_payload: Optional[dict] = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        has_traj = self.trajectory_id is not None
        has_import = self.import_payload is not None
        if has_traj == has_import:
            raise ValueError("provide exactly one of trajectory_id or import_payload")


@dataclass
class HygieneResult:
    accepted: bool
    reason: str
    ledger_receipt: str
    checks_passed: int = 0
    checks_total: int = 0
    strict_success: bool = False
    artifact_id: Optional[str] = None
    fresh_execution_id: Optional[str] = None
    duplicate_of: Optional[str] = None

    def __post_init__(self) -> None:
        if self.reason not in HYGIENE_REASONS:
            raise ValueError(f"unknown hygiene reason: {self.reason}")


@dataclass
class ScreenedArtifact:
    artifact_id: str
    criteria_hash: str
    task_class: str
    fresh_execution_id: str
    payload_uri: str
    payload_sha256: str
    payload_bytes: int
    checks_passed: int
    checks_total: int
    strict_success: bool
    leakage_scan_version: str
    producing_job_id: str
    ledger_receipt: str
    created_at: str
    parent_trajectory_id: Optional[str] = None
    import_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if bool(self.parent_trajectory_id) == bool(self.import_hash):
            raise ValueError("exactly one of parent_trajectory_id or import_hash")
        if not self.artifact_id.startswith("scr_"):
            raise ValueError("artifact_id must start with scr_")
        if self.payload_bytes > PAYLOAD_CAP_BYTES:
            raise ValueError("payload exceeds cap")


@dataclass
class NegativeCase:
    case_id: str
    reason: str
    criteria_hash: str
    payload_sha256: str
    ledger_receipt: str
    created_at: str
    stores_body: bool = False
    parent_trajectory_id: Optional[str] = None
    import_hash: Optional[str] = None


@dataclass
class CalibConstants:
    version: str
    family_quantile: dict
    approved_by: str
    approved_at: str
    fallback_d: float = 82.0
    partial_scale: float = 0.6

    def __post_init__(self) -> None:
        if not str(self.version).startswith("calib_"):
            raise ValueError("version must start with calib_")
        if not (0.0 < self.partial_scale < 1.0):
            raise ValueError("partial_scale must be in (0, 1)")
        if self.fallback_d <= 0:
            raise ValueError("fallback_d must be positive")


@dataclass
class ProgressScores:
    constants_version: str
    dense: float
    prioritized: float
    strict_success: bool


@dataclass
class ScoreSidecar:
    run_id: str
    legacy: float
    dense: float
    prioritized: float
    constants_version: str
    strict_success: bool
    task_class: str
    checks_passed: int
    checks_total: int


@dataclass
class TurnView:
    turn_id: str
    decision_steps: int
    observation_tokens: int = 0


@dataclass
class StepCredit:
    artifact_id: str
    turn_id: str
    credit: float
    gamma_turn: float
    constants_version: str


@dataclass
class SkillLineage:
    screened_artifact_id: str
    screened_payload_sha256: str
    criteria_hash: str
    recalibration_manifest_id: str = ""
    parent_skill_id: Optional[str] = None


@dataclass
class SkillVersion:
    skill_id: str
    task_class: str
    lineage: SkillLineage
    active: bool = False
    strict_success: bool = False


@dataclass
class RecalibrationManifest:
    manifest_id: str
    snapshot_id: str
    candidate_skill_ids: list
    passed: bool
    validator_failures: int
    sample_size: int
    duration_s: float
    ledger_receipt: str
    created_at: str
    duration_breach: bool = False


@dataclass
class StageEntry:
    snapshot_id: str
    stage: str
    retained: bool
    metrics: dict
    parent_receipt: str
    ledger_receipt: str = ""
    index_generation: int = 0
    active_skill_ids: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.stage not in STAGE_NAMES:
            raise ValueError(f"unknown stage: {self.stage}")


@dataclass
class ExecResult:
    fresh_execution_id: str
    actions: list
    observations: list
    turns: list
    checks_passed: int
    checks_total: int
    strict_success: bool
    elapsed_s: float = 0.0
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.error not in (None, "timeout", "sandbox_error"):
            raise ValueError(f"unknown exec error: {self.error}")


def to_jsonable(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
