"""What every node function receives besides the state (injected services)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, TypeVar

from contracts.ledger import LedgerAction, LedgerEntry
from contracts.run import MemoryBundle, RunState
from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus

if TYPE_CHECKING:
    from recertia.memory.affordance import AffordanceStore
    from recertia.memory.episodic import EpisodicStore
    from recertia.memory.semantic import FactStore
    from recertia.retrieval.pipeline import RetrievalExplanation
    from recertia.review import ReviewService
    from recertia.solver.apply import SkillApplicator
    from recertia.solver.model import ModelClient
    from recertia.solver.tools import ToolRuntime
    from recertia.solver.transcript import TranscriptStore

T = TypeVar("T")


class OperationRunner(Protocol):
    """The sole idempotency capability exposed to nodes."""

    def run_once(
        self,
        run_id: str,
        attempt_no: int,
        node: str,
        op_seq: int,
        fn: Callable[[], T],
        *,
        fencing_token: int | None = None,
    ) -> T: ...


class WorkspaceCapability(Protocol):
    """Snapshot/restore capability; nodes never receive the backing snapshot store."""

    def snapshot(self, workdir: Path, run_id: str, *, attempt_no: int) -> str: ...

    def restore(self, workdir: Path, snapshot_ref: str) -> None: ...


class LedgerCapability(Protocol):
    """Append-only audit capability required by terminal nodes."""

    def append(
        self,
        *,
        actor: str,
        action: LedgerAction,
        target: str,
        evidence: dict[Any, Any] | None = None,
        at: datetime | None = None,
    ) -> LedgerEntry: ...


class SearchCapability(Protocol):
    """Search + snapshot identity. Retrieve/evolve never see index writes."""

    def search(
        self,
        query: str,
        *,
        workdir: Path,
        env_fingerprint: dict[str, str] | None = None,
        readable_scopes: set[str] | None = None,
        suppress: bool = False,
    ) -> tuple[MemoryBundle, RetrievalExplanation]: ...

    def snapshot_id(self) -> str: ...


class IndexMaintenance(Protocol):
    """Index writes. Only the store node receives this."""

    def rebuild(
        self,
        entries: list[tuple[SkillVersion, SkillStatus, SkillStats]],
        *,
        library_fingerprint: str | None = None,
    ) -> str: ...

    def upsert(
        self,
        version: SkillVersion,
        status: SkillStatus,
        stats: SkillStats,
        *,
        library_fingerprint: str | None = None,
    ) -> str: ...

    def snapshot_id(self) -> str: ...


class RetrieverCapability(SearchCapability, IndexMaintenance, Protocol):
    """Full retriever. Prefer ``SearchCapability`` at call sites."""

    def is_fresh(self, library_fingerprint: str) -> bool: ...


class EpisodicRead(Protocol):
    """Read-only episodic lookups used by retrieve / evolve / plan."""

    def dead_ends_for(self, *, task_class: str | None = None, limit: int = 3) -> list[Any]: ...

    def solved_case_ids_for(self, *, task_class: str | None = None, limit: int = 3) -> list[str]: ...

    def list_index(self) -> list[dict[str, Any]]: ...

    def count_for_task_class(self, task_class: str | None) -> int: ...

    def approach_still_applies(self, dead_end: Any, *, current_approach: str) -> bool: ...


class FactRead(Protocol):
    """Read-only fact lookup used by retrieve."""

    def retrieve(self, query: str, *, scope: str = "project", limit: int = 10) -> list[Any]: ...


class SkillStoreCapability(Protocol):
    """T0-facing skill library writes: candidates only (approved lifecycle is gated elsewhere)."""

    def write_candidate(self, version: SkillVersion) -> SkillVersion: ...

    def get_version(self, skill_id: str, version: int) -> SkillVersion: ...

    def get_status(self, skill_id: str, version: int) -> SkillStatus: ...

    def iter_loaded(self) -> list[tuple[SkillVersion, SkillStatus, SkillStats]]: ...

    def library_fingerprint(self) -> str: ...


@dataclass
class NodeOutcome:
    """Updated state + chosen route predicate name (must be legal per contracts.graph)."""

    state: RunState
    route: str | None
    note: str | None = None


@dataclass
class NodeContext:
    """Injected services. Node functions are otherwise pure functions of ``state``."""

    run_id: str
    attempt_no: int
    node: str
    workdir: Path
    workspaces: WorkspaceCapability
    ledger: LedgerCapability
    ops: OperationRunner
    script: list[str] | None = None
    retriever: SearchCapability | None = None
    index: IndexMaintenance | None = None
    store: SkillStoreCapability | None = None
    env_fingerprint: dict[str, str] = field(default_factory=dict)
    # Solver / memory services
    tools: "ToolRuntime | None" = None
    model: "ModelClient | None" = None
    # Independent verifier identity; a solver model must never judge its own artifact.
    verifier_model: "ModelClient | None" = None
    transcripts: "TranscriptStore | None" = None
    applicator: "SkillApplicator | None" = None
    episodic: "EpisodicStore | None" = None
    affordances: "AffordanceStore | None" = None
    # Distillation / review services
    facts: "FactStore | None" = None
    reviewer: "ReviewService | None" = None
    one_off_log: Path | None = None
    deterministic_guide: bool = False
    patch_templates: Any = None
    # API ceiling applied after compile_goal. None keeps the contract maximum.
    max_criterion_timeout_s: int | None = None
    fencing_token: int | None = None

    def op_once(self, op_seq: int, fn: Callable[[], T]) -> T:
        return self.ops.run_once(
            self.run_id,
            self.attempt_no,
            self.node,
            op_seq,
            fn,
            fencing_token=self.fencing_token,
        )
