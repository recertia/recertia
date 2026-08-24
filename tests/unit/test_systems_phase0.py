"""Phase 0/1 systems instrumentation: six properties, caches, offload, prefix tree."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.policy import IsolationSettings, Policy, StateManagement
from contracts.trajectory import TrajectoryEvent
from recertia.ops.systems import (
    canonical_tool_key,
    component_class,
    redundancy_rate,
    rss_bytes,
    snapshot_from_events,
    workdir_bytes,
)
from recertia.policy_load import load_policy
from recertia.solver.registry import Tool, ToolResult
from recertia.solver.result_cache import ToolResultCache
from recertia.telemetry import SpanEvent, reset_telemetry
from recertia.trajectory.prefix_tree import build_prefix_tree, reconstructability_rate
from recertia.workspace.offload import WorkingSetOffload


def test_component_class_and_redundancy() -> None:
    assert component_class("retrieve") == "retrieve"
    assert component_class("validate") == "validate"
    assert component_class("solve") == "container"
    assert component_class("intake") == "control_plane"
    keys = ["a:1", "b:1", "a:1", "a:1"]
    assert redundancy_rate(keys) == 0.5
    assert redundancy_rate(["only"]) == 0.0


def test_rss_and_workdir_bytes(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "blob.bin").write_bytes(b"x" * 2048)
    assert workdir_bytes(work) >= 2048
    assert rss_bytes() > 0


def test_six_properties_from_events() -> None:
    events = [
        SpanEvent(
            name="node.finished",
            attributes={
                "node": "retrieve",
                "component_class": "retrieve",
                "latency_ms": 40.0,
                "rss_bytes": 1000,
                "workdir_bytes": 200,
                "idle_gap_ms": 12.0,
            },
        ),
        SpanEvent(
            name="node.finished",
            attributes={
                "node": "plan",
                "component_class": "llm",
                "latency_ms": 10.0,
                "rss_bytes": 1100,
                "workdir_bytes": 200,
                "idle_gap_ms": 3.0,
                "tokens": 8,
            },
        ),
        SpanEvent(
            name="tool.invoked",
            attributes={"canonical_key": canonical_tool_key("read_file", {"path": "a"})},
        ),
        SpanEvent(
            name="tool.invoked",
            attributes={"canonical_key": canonical_tool_key("read_file", {"path": "a"})},
        ),
        SpanEvent(
            name="retrieve.queried",
            attributes={"canonical_key": "snap:abc"},
        ),
    ]
    snap = snapshot_from_events(events)
    assert snap.hop_count == 2
    assert snap.non_llm_share > 0.5
    assert snap.peak_rss_bytes == 1100
    assert snap.tool_redundancy_rate == 0.5
    assert snap.median_idle_gap_ms == 7.5
    dumped = snap.as_dict()
    assert "control_plane_token_tax" in dumped


def test_tool_result_cache_hit_miss_and_write_skip() -> None:
    cache = ToolResultCache(ttl_s=60.0)
    reader = Tool(name="read_file", side_effect="read")
    writer = Tool(name="write_file", side_effect="write")
    ok = ToolResult(tool="read_file", ok=True, stdout="hello")
    cache.store(reader, {"path": "a"}, ok, snapshot_hash="s1")
    hit = cache.lookup(reader, {"path": "a"}, snapshot_hash="s1")
    assert hit is not None and hit.stdout == "hello"
    assert cache.lookup(reader, {"path": "a"}, snapshot_hash="s2") is None
    cache.store(writer, {"path": "a"}, ToolResult(tool="write_file", ok=True), snapshot_hash="s1")
    assert cache.lookup(writer, {"path": "a"}, snapshot_hash="s1") is None
    assert cache.stats.hits == 1


def test_tool_runtime_cache_and_invalidation_on_mutation(tmp_path: Path) -> None:
    from recertia.solver.claims import ClaimScheduler
    from recertia.solver.registry import ToolRegistry
    from recertia.solver.runtime import ToolRuntime

    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("one")
    registry = ToolRegistry()

    def read_handler(inputs: dict, workdir: Path) -> ToolResult:
        text = (workdir / inputs["path"]).read_text()
        return ToolResult(tool="read_file", ok=True, stdout=text)

    def write_handler(inputs: dict, workdir: Path) -> ToolResult:
        (workdir / inputs["path"]).write_text(inputs["text"])
        return ToolResult(tool="write_file", ok=True, stdout="ok")

    registry.register(Tool(name="read_file", side_effect="read"), read_handler)
    registry.register(Tool(name="write_file", side_effect="write"), write_handler)
    cache = ToolResultCache()
    runtime = ToolRuntime(
        registry,
        ClaimScheduler(),
        require_approval_for_non_read=False,
        result_cache=cache,
    )
    first = runtime.invoke("read_file", {"path": "a.txt"}, workdir=work, step_id="s1")
    second = runtime.invoke("read_file", {"path": "a.txt"}, workdir=work, step_id="s2")
    assert first.stdout == second.stdout == "one"
    assert cache.stats.hits == 1
    runtime.invoke(
        "write_file", {"path": "a.txt", "text": "two"}, workdir=work, step_id="s3"
    )
    third = runtime.invoke("read_file", {"path": "a.txt"}, workdir=work, step_id="s4")
    assert third.stdout == "two"
    assert cache.stats.misses >= 2


def test_offload_round_trip_hash_stable(tmp_path: Path) -> None:
    src = tmp_path / "work"
    src.mkdir()
    (src / "keep.txt").write_text("payload")
    nested = src / "dir"
    nested.mkdir()
    (nested / "inner.bin").write_bytes(b"abc")
    packs = WorkingSetOffload(tmp_path / "packs")
    handle = packs.pack(src, ref="run1-workdir")
    assert not src.exists()
    dest = tmp_path / "restored"
    packs.restore(handle, dest)
    assert (dest / "keep.txt").read_text() == "payload"
    assert (dest / "dir" / "inner.bin").read_bytes() == b"abc"
    handle2 = packs.pack(dest, ref="run1-again")
    dest2 = tmp_path / "restored2"
    packs.restore(handle2, dest2)
    assert (dest2 / "keep.txt").read_text() == "payload"


def test_prefix_tree_retries_are_siblings_and_reconstructable() -> None:
    now = datetime.now(timezone.utc)

    def ev(seq: int, node: str, kind: str, attempt: int) -> TrajectoryEvent:
        return TrajectoryEvent(
            run_id="r1",
            seq=seq,
            node=node,
            attempt_no=attempt,
            event_kind=kind,  # type: ignore[arg-type]
            at=now,
        )

    events = [
        ev(0, "plan", "plan_choice", 0),
        ev(1, "solve", "step_started", 1),
        ev(2, "classify_failure", "failure_classified", 1),
        ev(3, "solve", "step_started", 2),
        ev(4, "finalize", "terminal", 2),
    ]
    tree = build_prefix_tree(events, prune_dead=True)
    assert reconstructability_rate(events, tree) == 1.0
    assert tree.event_count == 3  # plan_choice + two step_started


def test_policy_state_management_defaults_offload_false() -> None:
    policy = load_policy()
    assert policy.state_management.idle_offload_enabled is False
    assert policy.state_management.tool_result_cache_enabled is True
    sm = StateManagement()
    assert sm.idle_offload_enabled is False
    Policy.model_validate(policy.model_dump())


def test_isolation_defaults_forbid_long_lived_computer() -> None:
    policy = load_policy()
    assert policy.isolation.allow_external_computer is False
    assert policy.isolation.long_lived_computer_backend is False
    assert policy.isolation.external_computer_allowlist == []
    assert policy.job_quota.computer_use_practice_share == 0.15
    IsolationSettings()
    Policy.model_validate(policy.model_dump())


def test_engine_emits_component_class(tmp_path: Path) -> None:
    from contracts.criteria import TaskCriterion
    from contracts.run import Task
    from recertia.graph.engine import GraphOrchestrator
    from recertia.telemetry import get_telemetry

    reset_telemetry(admin_actor="test-admin", tenant_id="local")
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    orch = GraphOrchestrator(tmp_path / "runs")
    try:
        orch.start(
            "run-sys",
            Task(task_id="t1", request="write output.txt", submitted_at=datetime.now(timezone.utc)),
            [
                TaskCriterion(
                    id="output-exists",
                    kind="command",
                    run="test -f output.txt",
                    source="caller",
                    weight=1.0,
                )
            ],
            workdir=workdir,
            max_steps=1,
        )
    finally:
        orch.close()
    finished = [
        e
        for e in get_telemetry().events
        if e.name == "node.finished" and e.attributes.get("run_id") == "run-sys"
    ]
    assert finished
    assert finished[0].attributes.get("component_class") == "control_plane"
    assert "rss_bytes" in finished[0].attributes
    assert "latency_ms" in finished[0].attributes


def test_operator_brief_lists_stuck_from_checkpoints(tmp_path: Path) -> None:
    from contracts.criteria import TaskCriterion
    from contracts.run import Task
    from recertia.graph.engine import GraphOrchestrator
    from recertia.ops.operator_brief import brief_from_runs_root

    workdir = tmp_path / "w"
    workdir.mkdir()
    runs = tmp_path / "runs"
    orch = GraphOrchestrator(runs)
    try:
        orch.start(
            "stuck-run",
            Task(task_id="t", request="pause me", submitted_at=datetime.now(timezone.utc)),
            [
                TaskCriterion(
                    id="x",
                    kind="command",
                    run="true",
                    source="caller",
                    weight=1.0,
                )
            ],
            workdir=workdir,
            max_steps=1,
        )
    finally:
        orch.close()
    brief = brief_from_runs_root(runs)
    assert any(j["run_id"] == "stuck-run" for j in brief.stuck_jobs)
