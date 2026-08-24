"""The graph orchestrator: routing, checkpointing, and resume (specs §5.3, M0).

Owns state transitions and budget accounting; checkpoints after every node so a run is
resumable at node granularity (M0 done-when: "killing the process mid-run and resuming
completes it from the last checkpoint with no operation double-applied"). Routing itself is
never decided here — it is read from ``contracts.graph``, the normative route table — this
class only validates that a node's chosen route is legal and walks the resulting edge.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from contracts.budget import Budget
from contracts.common import Arm
from contracts.criteria import TaskCriterion
from contracts.graph import legal_routes
from contracts.run import RouteEntry, RunManifest, RunState, Task, WorkspaceSnapshot
from contracts.trajectory import TrajectoryEvent
from recertia.graph.ops import OperationLedger
from recertia.graph.store import CheckpointStore
from recertia.ledger import HashChainLedger
from recertia.mea.runtime import audit_after_validate, bind_after_intake
from recertia.mea.store import AuditedStateStore
from recertia.memory.procedural.capability import CandidateSkillStoreAdapter
from recertia.nodes import NODE_FUNCS, NodeContext, NodeOutcome
from recertia.trajectory.emitter import TrajectoryEmitter
from recertia.trajectory.store import TrajectoryStore
from recertia.workspace import OffloadHandle, WorkspaceManager

if TYPE_CHECKING:
    from contracts.policy import Policy
    from recertia.memory.affordance import AffordanceStore
    from recertia.memory.episodic import EpisodicStore
    from recertia.memory.procedural.store import SkillStore
    from recertia.memory.semantic import FactStore
    from recertia.retrieval.pipeline import Retriever
    from recertia.review import ReviewService
    from recertia.solver.apply import SkillApplicator
    from recertia.solver.model import ModelClient
    from recertia.solver.tools import ToolRuntime
    from recertia.solver.transcript import TranscriptStore

MAX_GRAPH_STEPS = 500
"""A safety valve against a routing defect looping forever. Not a budget concept — a run that
legitimately needs this many node-hops has a routing bug, not a slow task."""


class RoutingError(RuntimeError):
    """A node chose an illegal route, or produced an ambiguous one it should have resolved."""


class GraphOrchestrator:
    def __init__(
        self,
        runs_root: Path | str,
        *,
        retriever: "Retriever | None" = None,
        store: "SkillStore | None" = None,
        env_fingerprint: dict[str, str] | None = None,
        tools: "ToolRuntime | None" = None,
        model: "ModelClient | None" = None,
        verifier_model: "ModelClient | None" = None,
        transcripts: "TranscriptStore | None" = None,
        applicator: "SkillApplicator | None" = None,
        episodic: "EpisodicStore | None" = None,
        affordances: "AffordanceStore | None" = None,
        facts: "FactStore | None" = None,
        reviewer: "ReviewService | None" = None,
        one_off_log: Path | None = None,
        policy: "Policy | None" = None,
        on_finalize: Callable[["RunState"], None] | None = None,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.checkpoints = CheckpointStore(self.runs_root / "checkpoints.db")
        self.ops = OperationLedger(self.runs_root / "operations.db")
        self.ledger = HashChainLedger(self.runs_root / "ledger.jsonl")
        self.workspaces = WorkspaceManager(self.runs_root / "snapshots")
        self.retriever = retriever
        self.store = store
        self.env_fingerprint = env_fingerprint or {}
        self.tools = tools
        self.model = model
        self.verifier_model = verifier_model
        self.transcripts = transcripts
        self.applicator = applicator
        self.episodic = episodic
        self.affordances = affordances
        self.facts = facts
        self.reviewer = reviewer
        self.one_off_log = one_off_log
        self.policy = policy
        self.on_finalize = on_finalize
        self.trajectories = TrajectoryStore(self.runs_root / "trajectories")
        self._trajectory_emitter = TrajectoryEmitter()
        self.audited_states = AuditedStateStore(self.runs_root / "audited_states")
        self.tenant_id = "local"
        self._last_hop_ended: float | None = None
        self._offload_handles: dict[str, OffloadHandle] = {}

    def close(self) -> None:
        self.checkpoints.close()
        self.ops.close()

    def _emit_trajectory(
        self,
        *,
        prior: RunState,
        new_state: RunState,
        node: str,
        attempt_no: int,
        route: str | None,
        note: str | None,
        extra_events: list[TrajectoryEvent] | None = None,
    ) -> None:
        """Best-effort trajectory append; never fails the run (ADR-0011)."""

        try:
            if not self.trajectories._meta_path(new_state.run_id).exists():
                self.trajectories.write_meta(
                    run_id=new_state.run_id,
                    task_id=new_state.task.task_id,
                    task_class=new_state.task.task_class,
                    arm=new_state.arm,
                    is_eval_fixture=new_state.task.is_eval_fixture,
                )
            events = self._trajectory_emitter.from_node_outcome(
                prior=prior,
                new_state=new_state,
                node=node,
                attempt_no=attempt_no,
                route=route,
                note=note,
            )
            if extra_events:
                events = [*events, *extra_events]
            if events:
                self.trajectories.append_many(new_state.run_id, events)
        except Exception:  # noqa: BLE001 — trajectory must not fail runs
            return

    def _record_eval_observation(self, state: RunState) -> None:
        """Best-effort finalize hook; never fails the run."""

        if self.on_finalize is None or state.terminal is None:
            return
        try:
            self.on_finalize(state)
        except Exception:  # noqa: BLE001 — eval recording must not fail runs
            return

    def _build_node_context(
        self,
        *,
        state: RunState,
        node_name: str,
        workdir: Path,
        script: list[str] | None,
        attempt_no: int,
    ) -> NodeContext:
        return NodeContext(
            run_id=state.run_id,
            attempt_no=attempt_no,
            node=node_name,
            workdir=workdir,
            workspaces=self.workspaces,
            ledger=self.ledger,
            ops=self.ops,
            script=script,
            retriever=self.retriever,
            index=self.retriever,
            store=(
                CandidateSkillStoreAdapter(self.store) if self.store is not None else None
            ),
            env_fingerprint=self.env_fingerprint,
            tools=self.tools,
            model=self.model,
            verifier_model=self.verifier_model,
            transcripts=self.transcripts,
            applicator=self.applicator,
            episodic=self.episodic,
            affordances=self.affordances,
            facts=self.facts,
            reviewer=self.reviewer,
            one_off_log=self.one_off_log,
            deterministic_guide=bool(
                self.policy is not None and self.policy.improvement.deterministic_guide
            ),
        )

    def _choose_route(self, node_name: str, outcome: NodeOutcome):
        legal = legal_routes(node_name, outcome.state)
        if outcome.route is not None:
            chosen = next((r for r in legal if r.predicate_name == outcome.route), None)
            if chosen is None:
                raise RoutingError(
                    f"node {node_name!r} chose illegal route {outcome.route!r}; "
                    f"legal routes for this state: {[r.predicate_name for r in legal]}"
                )
            return chosen
        if len(legal) != 1:
            raise RoutingError(
                f"node {node_name!r} produced an ambiguous state with no explicit route: "
                f"{[r.predicate_name for r in legal]}; the node must choose"
            )
        return legal[0]

    def _ensure_pre_solve_snapshot(
        self, state: RunState, chosen, workdir: Path
    ) -> RunState:
        if chosen.target != "solve" or state.workspace_snapshots:
            return state
        ref = self.workspaces.snapshot(workdir, state.run_id, attempt_no=0)
        return state.model_copy(
            update={
                "workspace_snapshots": [
                    WorkspaceSnapshot(attempt_no=0, snapshot_ref=ref, restored=False)
                ]
            }
        )


    def start(
        self,
        run_id: str,
        task: Task,
        criteria: list[TaskCriterion],
        *,
        budget: Budget | None = None,
        workdir: Path | str,
        script: list[str] | None = None,
        max_steps: int | None = None,
        arm: Arm = "treatment",
        manifest: RunManifest | None = None,
    ) -> RunState:
        """Start a brand-new run at ``intake``.

        ``arm`` is assigned by the caller (CLI / harness / ablation sampler). Nodes never
        import the T3 ablation module (ADR-0005). ``manifest`` pins model/library snapshot
        identity for measurement (M4).
        """

        state = RunState(
            run_id=run_id,
            task=task,
            criteria=criteria,
            budget=budget or Budget(),
            arm=arm,
            manifest=manifest or RunManifest(),
        )
        from recertia.telemetry import emit_in_run, telemetry_run

        with telemetry_run(tenant_id=self.tenant_id, run_id=run_id):
            emit_in_run("run.started", task_class=task.task_class, arm=arm)
            return self._execute(
                state, "intake", workdir=Path(workdir), script=script, max_steps=max_steps
            )

    def resume(
        self,
        run_id: str,
        *,
        workdir: Path | str,
        script: list[str] | None = None,
        max_steps: int | None = None,
    ) -> RunState:
        """Resume from the last checkpoint. A no-op if the run already reached ``finalize``."""

        latest = self.checkpoints.latest(run_id)
        if latest is None:
            raise ValueError(f"no checkpoint found for run {run_id!r}")
        seq, _, next_node, state = latest
        if next_node is None:
            return state
        self._maybe_restore_idle(run_id, Path(workdir))
        return self._execute(
            state,
            next_node,
            workdir=Path(workdir),
            script=script,
            max_steps=max_steps,
            next_seq=seq + 1,
        )

    def _execute(
        self,
        state: RunState,
        node_name: str,
        *,
        workdir: Path,
        script: list[str] | None,
        max_steps: int | None = None,
        next_seq: int | None = None,
    ) -> RunState:
        if next_seq is None:
            latest_seq = self.checkpoints.latest_seq(state.run_id)
            next_seq = (latest_seq + 1) if latest_seq is not None else 0
        steps_taken = 0

        while True:
            steps_taken += 1
            if steps_taken > MAX_GRAPH_STEPS:
                raise RoutingError(
                    f"run {state.run_id!r} exceeded {MAX_GRAPH_STEPS} graph steps; likely a routing defect"
                )
            if max_steps is not None and steps_taken > max_steps:
                # Pause = Recertia idle. quiet_threshold_s is telemetry, not this trigger.
                self._maybe_offload_idle(state, workdir)
                return state

            attempt_no_for_ctx = state.attempt_no + 1 if node_name == "solve" else state.attempt_no
            hop_started = time.monotonic()
            idle_gap_ms = 0.0
            if self._last_hop_ended is not None:
                idle_gap_ms = (hop_started - self._last_hop_ended) * 1000.0
            from recertia.ops.systems import component_class, rss_bytes, workdir_bytes
            from recertia.telemetry import emit_in_run, telemetry_run

            with telemetry_run(tenant_id=self.tenant_id, run_id=state.run_id):
                emit_in_run(
                    "node.started",
                    node=node_name,
                    component_class=component_class(node_name),
                )
                ctx = self._build_node_context(
                    state=state,
                    node_name=node_name,
                    workdir=workdir,
                    script=script,
                    attempt_no=attempt_no_for_ctx,
                )
                outcome = NODE_FUNCS[node_name](state, ctx)
                new_state = outcome.state
                hop_ms = (time.monotonic() - hop_started) * 1000.0
                emit_in_run(
                    "node.finished",
                    node=node_name,
                    component_class=component_class(node_name),
                    latency_ms=round(hop_ms, 3),
                    rss_bytes=rss_bytes(),
                    workdir_bytes=workdir_bytes(workdir),
                    idle_gap_ms=round(idle_gap_ms, 3),
                    tokens=int(getattr(new_state.spent, "tokens", 0) or 0),
                )
            self._last_hop_ended = time.monotonic()
            if new_state.spent.versions_written > new_state.budget.max_versions_written:
                raise RoutingError(
                    f"run {state.run_id!r} wrote {new_state.spent.versions_written} "
                    f"versions; budget.max_versions_written="
                    f"{new_state.budget.max_versions_written}"
                )

            if node_name == "finalize":
                self._emit_trajectory(
                    prior=state,
                    new_state=new_state,
                    node=node_name,
                    attempt_no=attempt_no_for_ctx,
                    route=outcome.route,
                    note=outcome.note,
                )
                self.checkpoints.save(state.run_id, next_seq, node_name, None, new_state)
                self._record_eval_observation(new_state)
                from recertia.telemetry import emit_in_run, telemetry_run

                with telemetry_run(tenant_id=self.tenant_id, run_id=state.run_id):
                    emit_in_run("run.finished", terminal=new_state.terminal)
                return new_state

            chosen = self._choose_route(node_name, outcome)
            new_state = self._ensure_pre_solve_snapshot(new_state, chosen, workdir)

            route_entry = RouteEntry(
                node=node_name,
                route=chosen.predicate_name,
                reason=outcome.note or chosen.description,
                attempt_no=attempt_no_for_ctx,
                at=datetime.now(timezone.utc),
            )
            new_state = new_state.model_copy(update={"route_log": [*new_state.route_log, route_entry]})

            extra_events: list[TrajectoryEvent] = []
            if node_name == "intake":
                new_state = bind_after_intake(
                    new_state,
                    policy=self.policy,
                    store=self.audited_states,
                    ledger=self.ledger,
                )
            elif node_name == "validate":
                new_state, delta = audit_after_validate(
                    new_state,
                    store=self.audited_states,
                    attempt_no=attempt_no_for_ctx,
                )
                if delta is not None:
                    extra_events.append(
                        self._trajectory_emitter.from_auditor_delta(
                            new_state,
                            node=node_name,
                            attempt_no=attempt_no_for_ctx,
                            delta=delta,
                        )
                    )

            self._emit_trajectory(
                prior=state,
                new_state=new_state,
                node=node_name,
                attempt_no=attempt_no_for_ctx,
                route=chosen.predicate_name,
                note=outcome.note,
                extra_events=extra_events,
            )
            self.checkpoints.save(state.run_id, next_seq, node_name, chosen.target, new_state)
            next_seq += 1
            state = new_state
            node_name = chosen.target

    def _offload_enabled(self) -> bool:
        policy = self.policy
        if policy is None:
            return False
        sm = getattr(policy, "state_management", None)
        return bool(sm is not None and sm.idle_offload_enabled)

    def _maybe_offload_idle(self, state: RunState, workdir: Path) -> None:
        if not self._offload_enabled():
            return
        if not workdir.exists():
            return
        from recertia.telemetry import emit_in_run, telemetry_run
        from recertia.workspace.offload import WorkingSetOffload

        packs = WorkingSetOffload(self.runs_root / "offload")
        hop_started = time.monotonic()
        handle = packs.pack(workdir, ref=f"{state.run_id}-workdir")
        self._offload_handles[state.run_id] = handle
        packs.write_sidecar(state.run_id, handle)
        with telemetry_run(tenant_id=self.tenant_id, run_id=state.run_id):
            emit_in_run(
                "idle.offload",
                bytes_offloaded=handle.bytes_offloaded,
                original_bytes=handle.original_bytes,
                latency_ms=round((time.monotonic() - hop_started) * 1000.0, 3),
            )

    def _maybe_restore_idle(self, run_id: str, workdir: Path) -> None:
        from recertia.telemetry import emit_in_run, telemetry_run
        from recertia.workspace.offload import OffloadError, WorkingSetOffload

        packs = WorkingSetOffload(self.runs_root / "offload")
        handle = self._offload_handles.get(run_id) or packs.read_sidecar(run_id)
        if handle is None:
            return

        hop_started = time.monotonic()
        with telemetry_run(tenant_id=self.tenant_id, run_id=run_id):
            try:
                packs.restore(handle, workdir)
            except OffloadError as exc:
                raise RoutingError(str(exc)) from exc
            emit_in_run(
                "idle.restore",
                bytes_offloaded=handle.bytes_offloaded,
                latency_ms=round((time.monotonic() - hop_started) * 1000.0, 3),
            )
        self._offload_handles.pop(run_id, None)
        packs.drop_sidecar(run_id)
