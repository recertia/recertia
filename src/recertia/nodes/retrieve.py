"""``retrieve``: federated memory query (specs §4, §5).

Procedural plane via Retriever (M1). Episodic dead ends + solved cases (M2). Affordance
tool cautions (M2). Control arm still returns an empty suppressed bundle.
"""

from __future__ import annotations

from contracts.run import MemoryBundle, RunState, Task
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.retrieval.bundle import assemble_bundle
from recertia.retrieval.config import RetrievalConfig


def retrieval_query(*, request: str | None, goal_context: str | None, goal_terms: str = "") -> str:
    """Build a non-None retrieval query from request, goal context, or goal terms."""

    if request is not None and request.strip():
        return request.strip()
    if goal_context is not None and goal_context.strip():
        return goal_context.strip()
    if goal_terms.strip():
        return goal_terms.strip()
    return ""


def _query_for(task: Task) -> str:
    """Non-None retrieval string for goal-only runs (no request / context)."""

    goal_terms = ""
    if task.goal is not None:
        parts: list[str] = []
        for desired in task.goal.desired:
            parts.append(desired.id)
            if desired.path:
                parts.append(desired.path)
            if desired.run:
                parts.append(desired.run)
            if desired.pattern:
                parts.append(desired.pattern)
        goal_terms = " ".join(parts)
        context = task.goal.context
    else:
        context = None
    return retrieval_query(request=task.request, goal_context=context, goal_terms=goal_terms)


def retrieve(state: RunState, ctx: NodeContext) -> NodeOutcome:
    if state.arm == "control":
        # Still pin the library snapshot so control observations compare against the
        # same index the treatment arm would have queried.
        snapshot_id = None
        if ctx.retriever is not None:
            snapshot_id = ctx.retriever.snapshot_id()
        bundle = MemoryBundle(suppressed=True)
        updates: dict = {"bundle": bundle}
        if snapshot_id is not None:
            updates["manifest"] = state.manifest.model_copy(
                update={"index_snapshot_id": snapshot_id}
            )
        new_state = state.model_copy(update=updates)
        return NodeOutcome(
            state=new_state,
            route="always",
            note=f"control arm: retrieval suppressed snapshot={snapshot_id}",
        )

    query = _query_for(state.task)
    skills = []
    snapshot_id = None
    dropped = 0
    config = RetrievalConfig()
    if ctx.retriever is not None:
        bundle, explanation = ctx.retriever.search(
            query,
            workdir=ctx.workdir,
            env_fingerprint=ctx.env_fingerprint,
            suppress=False,
        )
        skills = list(bundle.skills)
        snapshot_id = explanation.snapshot_id
        dropped = len(explanation.dropped)
        config = getattr(ctx.retriever, "config", config)

    bundle = assemble_bundle(
        skills=skills,
        query=query,
        task_class=state.task.task_class,
        episodic=ctx.episodic,
        facts=ctx.facts,
        affordances=ctx.affordances,
        config=config,
    )
    state_updates: dict = {"bundle": bundle}
    if snapshot_id is not None:
        state_updates["manifest"] = state.manifest.model_copy(
            update={"index_snapshot_id": snapshot_id}
        )
    new_state = state.model_copy(update=state_updates)
    note = (
        f"snapshot={snapshot_id} skills={len(skills)} dead_ends={len(bundle.dead_ends)} "
        f"cautions={len(bundle.tool_cautions)} dropped={dropped}"
    )
    return NodeOutcome(state=new_state, route="always", note=note)
