"""Default runtime wiring for CLI and API run startup.

Builds a ``GraphOrchestrator`` with the memory / retrieval / tool stack needed for
library apply paths — not a bare checkpoint engine.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from contracts.run import RunManifest
from recertia.config import ModelConfig, load_model_config
from recertia.governance.sandbox import ApprovalGate
from recertia.memory.affordance import AffordanceStore
from recertia.memory.episodic import EpisodicStore
from recertia.memory.procedural.store import SkillStore
from recertia.memory.semantic import FactStore
from recertia.retrieval.index import SkillIndex
from recertia.retrieval.pipeline import Retriever
from recertia.solver.apply import SkillApplicator
from recertia.solver.factory import build_solver_and_verifier
from recertia.solver.tools import ClaimScheduler, ToolRuntime, default_registry
from recertia.solver.transcript import TranscriptStore
from recertia.workspace import WorkspaceManager

if TYPE_CHECKING:
    from recertia.graph.engine import GraphOrchestrator
    from recertia.solver.model import ModelClient


@dataclass
class OrchestratorBundle:
    """Orchestrator plus closable index handle and a pinned run manifest template."""

    orchestrator: "GraphOrchestrator"
    index: SkillIndex
    model_config: ModelConfig | None = None

    def close(self) -> None:
        self.orchestrator.close()
        self.index.close()

    def run_manifest(self, *, seed: int | None = None) -> RunManifest:
        """Pin provider/model/index/library identity for measurement (P0-4)."""

        return build_run_manifest(
            self,
            model_config=self.model_config,
            seed=seed,
        )


def _git_head(cwd: Path | None = None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    digest = proc.stdout.strip()
    return digest or None


def build_run_manifest(
    bundle: OrchestratorBundle,
    *,
    model_config: ModelConfig | None = None,
    seed: int | None = None,
) -> RunManifest:
    """Build a fully-pinned :class:`RunManifest` from the live orchestrator stack."""

    cfg = model_config or bundle.model_config or load_model_config()
    model = bundle.orchestrator.model
    provider = (model.provider if model is not None else None) or cfg.provider
    model_id = (model.model_id if model is not None else None) or cfg.model_id
    snapshot_id = bundle.index.snapshot_id()
    if not snapshot_id:
        store = bundle.orchestrator.store
        if store is not None and hasattr(store, "library_fingerprint"):
            snapshot_id = f"fp:{store.library_fingerprint()}"
    library_commit = os.environ.get("RECERTIA_LIBRARY_COMMIT") or _git_head()
    if library_commit is None and snapshot_id:
        library_commit = str(snapshot_id)
    return RunManifest(
        model=provider,
        model_version=model_id,
        index_snapshot_id=str(snapshot_id) if snapshot_id else None,
        library_commit=library_commit,
        policy_version=os.environ.get("RECERTIA_POLICY_VERSION"),
        seed=seed,
    )


def build_default_orchestrator(
    runs_root: Path | str,
    *,
    skills_root: Path | str = Path("skills"),
    facts_root: Path | str = Path("facts"),
    index_path: Path | str | None = None,
    golden_root: Path | str | None = None,
    env_fingerprint: dict[str, str] | None = None,
    approve_default_tools: bool = True,
    model_config: ModelConfig | None = None,
    model: "ModelClient | None" = None,
    verifier_model: "ModelClient | None" = None,
) -> OrchestratorBundle:
    """Wire SkillStore, Retriever, tools, applicator, episodic/facts/affordances.

    When ``golden_root`` is set, also wires a ``ReviewService`` so reusable drafts can
    be promoted. Without it, distill keeps solved runs as ``one_off`` (draft retained).

    Model wiring: explicit ``model`` / ``verifier_model`` win. Otherwise
    ``model_config`` (or env via :func:`load_model_config`) builds clients. Stub
    provider leaves model unset unless ``RECERTIA_ALLOW_STUB_MODEL=1``, so scratch
    fails loud instead of silently no-oping.
    """

    from recertia.graph.engine import GraphOrchestrator
    from recertia.memory.procedural.lineage import LineageServices
    from recertia.policy_load import load_policy
    from recertia.review import ReviewService

    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    skills_root = Path(skills_root)
    facts_root = Path(facts_root)
    index_path = Path(index_path) if index_path is not None else runs_root / "skill_index.db"

    cfg = model_config if model_config is not None else load_model_config()
    if model is None and verifier_model is None:
        # Stub → None (fail-loud scratch). Non-stub misconfig raises ModelConfigError.
        model, verifier_model = build_solver_and_verifier(cfg)

    lineage = LineageServices.open(runs_root / "lineage")
    store = SkillStore(
        skills_root,
        lineage_index=lineage.index,
        revoke_queue=lineage.queue,
    )
    policy = load_policy()
    index = SkillIndex(index_path)
    # Full rebuilds cost one JSON parse + embed per skill version. When the persisted
    # index already matches the on-disk library (the common case: startup, per-request
    # API wiring), skip straight to serving; a stat-only fingerprint decides.
    fingerprint = store.library_fingerprint()
    if not index.is_fresh(fingerprint):
        index.rebuild(store.iter_loaded(), library_fingerprint=fingerprint)
    retriever = Retriever(index)
    sm = policy.state_management
    retriever.result_cache.enabled = sm.retrieval_cache_enabled
    retriever.result_cache.ttl_s = sm.retrieval_cache_ttl_s

    registry = default_registry()
    gate = ApprovalGate()
    if approve_default_tools:
        for name in registry.names():
            gate.approve(name, actor="runtime-bootstrap", reason="default offline grant")
    from recertia.solver.result_cache import ToolResultCache

    tool_cache = ToolResultCache(
        ttl_s=sm.tool_result_cache_ttl_s,
        enabled=sm.tool_result_cache_enabled,
    )
    tools = ToolRuntime(
        registry,
        ClaimScheduler(),
        approval_gate=gate,
        model=model,
        result_cache=tool_cache,
    )
    workspaces = WorkspaceManager(runs_root / "snapshots")
    transcripts = TranscriptStore(runs_root / "transcripts")
    applicator = SkillApplicator(tools, workspaces)

    reviewer = None
    if golden_root is not None:
        reviewer = ReviewService(
            runs_root / "review",
            golden_root=Path(golden_root),
            runs_root=runs_root / "review-runs",
        )

    def _record_eval(state) -> None:
        if state.terminal is None:
            return
        try:
            from recertia.evals.store import EvalStore, ObservationError

            eval_store = EvalStore(runs_root / "evals.db")
            try:
                eval_store.append_run(state)
            except ObservationError:
                return
            finally:
                eval_store.close()
        except Exception:  # noqa: BLE001 — eval recording must not fail runs
            return

    orch = GraphOrchestrator(
        runs_root,
        store=store,
        retriever=retriever,
        tools=tools,
        model=model,
        verifier_model=verifier_model,
        transcripts=transcripts,
        applicator=applicator,
        episodic=EpisodicStore(runs_root / "episodic"),
        affordances=AffordanceStore(runs_root / "affordances.json"),
        facts=FactStore(facts_root),
        reviewer=reviewer,
        # Empty fingerprint: only mismatch when both sides declare a tool.
        env_fingerprint=env_fingerprint if env_fingerprint is not None else {},
        policy=policy,
        on_finalize=_record_eval,
    )
    # Share the same WorkspaceManager the applicator uses for attempt isolation.
    orch.workspaces = workspaces
    return OrchestratorBundle(orchestrator=orch, index=index, model_config=cfg)


def resolve_task_class(
    *,
    explicit: str | None,
    goal_task_class: str | None,
    default: str = "repo-chore",
) -> str:
    """Prefer caller override, then Goal.task_class, then the system default."""

    for candidate in (explicit, goal_task_class):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return default
