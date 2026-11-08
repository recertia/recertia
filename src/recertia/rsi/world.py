"""In-memory planes. Maps spec stores onto one process without a second memory plane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from recertia.rsi.ledger import Ledger
from recertia.rsi.models import (
    CalibConstants,
    NegativeCase,
    ScoreSidecar,
    ScreenedArtifact,
    SkillVersion,
    StepCredit,
)


@dataclass
class Flags:
    """Defaults are the safe (off) position. Spec section 13."""

    hygiene_enabled: bool = False
    calibrated_scoring: bool = False
    phase4_retirement: bool = False

    @classmethod
    def from_env(cls) -> "Flags":
        def on(name: str) -> bool:
            return os.environ.get(name, "0") == "1"

        return cls(
            hygiene_enabled=on("RECERTIA_HYGIENE_ENABLED"),
            calibrated_scoring=on("RECERTIA_CALIBRATED_SCORING"),
            phase4_retirement=on("RECERTIA_PHASE4_RETIREMENT"),
        )


@dataclass
class LockedCriteria:
    criteria_hash: str
    task_class: str
    checks: list


@dataclass
class StoredTrajectory:
    trajectory_id: str
    criteria_hash: str
    task_class: str
    public: dict
    private: dict = field(default_factory=dict)


class PolicyStore:
    """T2. CalibConstants land here only through a human-gate token."""

    def __init__(self) -> None:
        self._gates: set[str] = set()
        self.versions: dict[str, CalibConstants] = {}
        self.active_version: Optional[str] = None

    def approve_gate(self, token: str) -> None:
        if not token:
            raise ValueError("empty human-gate token")
        self._gates.add(token)

    def put_constants(self, constants: CalibConstants, token: str) -> None:
        if token not in self._gates:
            raise PermissionError("T2 human gate required to write CalibConstants")
        if constants.version in self.versions:
            raise ValueError("constants versions are immutable")
        self.versions[constants.version] = constants
        self.active_version = constants.version

    def get(self, version: Optional[str] = None) -> Optional[CalibConstants]:
        key = version or self.active_version
        if key is None:
            return None
        return self.versions.get(key)


class World:
    def __init__(self, job_id: str = "job-local") -> None:
        self.job_id = job_id
        self.ledger = Ledger()
        self.flags = Flags()
        self.policy = PolicyStore()
        self.criteria: dict[str, LockedCriteria] = {}
        self.trajectories: dict[str, StoredTrajectory] = {}
        self.blobs: dict[str, bytes] = {}
        self.screened: dict[str, ScreenedArtifact] = {}
        self.negatives: dict[str, NegativeCase] = {}
        self.skills: dict[str, SkillVersion] = {}
        self.sidecars: dict[str, ScoreSidecar] = {}
        self.credits: list[StepCredit] = []
        self.manifests: dict[str, object] = {}
        self.idempotency: dict[str, object] = {}
        self.accepted_keys: dict[tuple, str] = {}
        self.review_queue: list[dict] = []
        self.incidents: list[dict] = []
        self.allow_policies = {"sandbox-default"}
        self.practice_job_s = 100.0
        self.sandbox_calls = 0
        self.exec_seconds: list[float] = []
        self.reject_counts: dict[str, int] = {}
        self._gamma_turn = 0.95

    def register_criteria(self, criteria_hash: str, task_class: str, checks: list) -> None:
        self.criteria[criteria_hash] = LockedCriteria(criteria_hash, task_class, list(checks))

    def put_trajectory(self, traj: StoredTrajectory) -> None:
        self.trajectories[traj.trajectory_id] = traj

    def active_skill_ids(self) -> list[str]:
        return sorted(sid for sid, sk in self.skills.items() if sk.active)

    def active_cap_occupancy(self) -> int:
        """Screened artifacts and negative cases do not count. Spec R3, R4."""
        return len(self.active_skill_ids())
