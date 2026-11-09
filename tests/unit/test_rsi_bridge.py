"""Phase B bridge. The flag stays off unless a test opts in."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import MemoryBundle, RunManifest, RunState, SkillCandidateRef, Task
from contracts.trajectory_import import TrajectoryImport
from recertia.app.library import LibraryService
from recertia.distill.imported import DistillRejected, distill_imported
from recertia.ledger.hashchain import HashChainLedger
from recertia.memory.episodic import EpisodicStore
from recertia.memory.procedural.store import SkillStore
from recertia.nodes.context import NodeContext
from recertia.nodes.distill import _author_or_reject
from recertia.nodes.retrieve import retrieve
from recertia.nodes.rsi_bridge import (
    live_hygiene_block,
    production_executor,
    run_durable_hygiene,
)
from recertia.ports.identity import Principal
from recertia.rsi.models import ExecResult, HygieneRequest

CRITERIA = "criteria-locked-1"
TASK = "repo-chore"
MARKER = "UNIQUE_PAYLOAD_MARKER_XYZ"


class _Spaces:
    def snapshot(self, workdir, run_id, *, attempt_no):
        return "snap"

    def restore(self, workdir, snapshot_ref):
        return None


class _Ops:
    def run_once(self, run_id, attempt_no, node, op_seq, fn, fencing_token=None, incarnation=1):
        return fn()


def _ok(observations=None, *, strict=True, checks_passed=1, checks_total=1):
    def run(public):
        blob = json.dumps(public)
        if "private" in public or "PRIV_GUIDANCE" in blob or "do-not-forward" in blob:
            raise AssertionError("private bag forwarded to sandbox")
        return ExecResult(
            fresh_execution_id="fx-1",
            actions=["echo ok"],
            observations=observations if observations is not None else ["ok"],
            turns=[],
            checks_passed=checks_passed,
            checks_total=checks_total,
            strict_success=strict,
        )

    return run


def _req(key: str = "idem-1") -> HygieneRequest:
    return HygieneRequest(
        criteria_hash=CRITERIA,
        task_class=TASK,
        sandbox_policy_id="sandbox-default",
        idempotency_key=key,
        import_payload={"cmd": "echo ok", "request": "write a note"},
    )


def _pair(tmp_path: Path):
    store = EpisodicStore(tmp_path / "episodic")
    ledger = HashChainLedger(tmp_path / "ledger" / "ledger.jsonl")
    return store, ledger


def _state() -> RunState:
    return RunState(
        run_id="run-1",
        task=Task(
            task_id="t",
            request="do a repo chore please",
            task_class=TASK,
            submitted_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
        ),
        manifest=RunManifest(),
        criteria=[TaskCriterion(id="c1", kind="command", run="test -f x", source="caller", timeout_s=30)],
        strategy="scratch",
    )


def _ctx(tmp_path: Path, store: EpisodicStore, ledger: HashChainLedger) -> NodeContext:
    return NodeContext(
        run_id="run-1",
        attempt_no=1,
        node="distill",
        workdir=tmp_path,
        workspaces=_Spaces(),
        ledger=ledger,
        ops=_Ops(),
        episodic=store,
        script=["echo ok"],
    )


def _distillable() -> dict:
    return {
        "import_id": "imp-d",
        "source": "synthetic",
        "source_ref": "fixture://repro",
        "captured_at": datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat(),
        "environment": {"os": "linux", "tools": ["shell"], "network_policy": "none"},
        "steps": [
            {
                "seq": 0,
                "action": "python -c \"open('x','w').write('ok')\"",
                "input": "python -c \"open('x','w').write('ok')\"",
            }
        ],
        "outcome": "solved",
        "criteria_snapshot": [
            {"id": "x-exists", "kind": "command", "run": "test -f x", "source": "caller", "weight": 1.0}
        ],
        "provenance": {"source": "synthetic", "source_ref": "fixture://repro"},
        "artifacts": [],
        "reexecutable": True,
        "require_auditor_reverify": True,
    }


def test_missing_budget_unknown_policy_and_private_bag_do_not_start(tmp_path: Path) -> None:
    calls: list[object] = []

    def runner(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("runner started")

    public = {"cmd": "echo ok"}
    missing = production_executor(
        public, timeout_s=None, policy_id="sandbox-default", workdir=tmp_path, runner=runner
    )
    unknown = production_executor(
        public, timeout_s=30, policy_id="sandbox-other", workdir=tmp_path, runner=runner
    )
    quiet = production_executor(
        {"request": "no command"},
        timeout_s=30,
        policy_id="sandbox-default",
        workdir=tmp_path,
        runner=runner,
    )
    assert missing.error == "sandbox_error"
    assert unknown.error == "sandbox_error"
    assert quiet.error == "sandbox_error"
    assert calls == []
    with pytest.raises(ValueError, match="private"):
        production_executor(
            {"cmd": "echo ok", "private": {"note": "do-not-forward"}},
            timeout_s=30,
            policy_id="sandbox-default",
            workdir=tmp_path,
            runner=runner,
        )
    with pytest.raises(ValueError, match="host path"):
        production_executor(
            {"cmd": "cat /home/example/secret"},
            timeout_s=30,
            policy_id="sandbox-default",
            workdir=tmp_path,
            runner=runner,
        )
    assert calls == []


def test_restart_replays_idempotency_without_a_second_run(tmp_path: Path) -> None:
    calls = {"n": 0}

    def executor(public):
        calls["n"] += 1
        return _ok(observations=[MARKER])(public)

    store, ledger = _pair(tmp_path)
    first = run_durable_hygiene(_req("same"), store=store, ledger=ledger, executor=executor)
    assert first.accepted
    assert first.artifact_id is not None
    assert first.artifact_id.startswith("scr_")
    blob = store.root / "blobs"
    assert any(blob.glob("*.json.gz"))
    ledger_text = ledger.path.read_text(encoding="utf-8")
    case_text = "\n".join(path.read_text(encoding="utf-8") for path in (store.root / "cases").glob("*.json"))
    assert MARKER not in ledger_text
    assert MARKER not in case_text
    assert "/home/" not in ledger_text

    restarted = HashChainLedger(ledger.path)
    again = run_durable_hygiene(
        _req("same"),
        store=EpisodicStore(store.root),
        ledger=restarted,
        executor=executor,
    )
    assert again.artifact_id == first.artifact_id
    assert calls["n"] == 1

    other = run_durable_hygiene(
        _req("other"),
        store=EpisodicStore(store.root),
        ledger=HashChainLedger(ledger.path),
        executor=executor,
    )
    assert other.reason == "duplicate"
    assert other.duplicate_of == first.artifact_id
    assert calls["n"] == 2
    assert len(list(blob.glob("*.json.gz"))) == 1
    assert len(list((store.root / "cases").glob("*.json"))) >= 1


def test_distinct_payload_is_durable_and_not_a_duplicate(tmp_path: Path) -> None:
    calls = {"n": 0}

    def executor(public):
        calls["n"] += 1
        return _ok(observations=[f"payload-{calls['n']}"])(public)

    store, ledger = _pair(tmp_path)
    first = run_durable_hygiene(_req("a"), store=store, ledger=ledger, executor=executor)
    second = run_durable_hygiene(_req("b"), store=store, ledger=ledger, executor=executor)
    assert first.accepted and second.accepted
    assert first.artifact_id != second.artifact_id
    assert calls["n"] == 2


def test_leakage_stores_no_body(tmp_path: Path) -> None:
    def executor(public):
        return _ok(observations=["PRIV_GUIDANCE: SECRET_MARKER_XYZ"])(public)

    store, ledger = _pair(tmp_path)
    result = run_durable_hygiene(_req("leak"), store=store, ledger=ledger, executor=executor)
    assert result.reason == "leakage"
    assert result.accepted is False
    dumped = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert "SECRET_MARKER_XYZ" not in dumped
    assert "PRIV_GUIDANCE" not in dumped
    assert not list((store.root / "blobs").glob("*"))


def test_flag_off_does_not_call_the_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_HYGIENE_ENABLED", raising=False)
    calls: list[int] = []

    def runner(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("runner started")

    store, ledger = _pair(tmp_path)
    hold = live_hygiene_block(_state(), _ctx(tmp_path, store, ledger), runner=runner)
    assert hold is None
    assert calls == []


def test_flag_on_verifier_fail_does_not_author(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_HYGIENE_ENABLED", "1")
    store, ledger = _pair(tmp_path)
    calls: list[int] = []

    def runner(*_args, **_kwargs):
        calls.append(1)
        return ExecResult(
            fresh_execution_id="fx-fail",
            actions=["echo ok"],
            observations=["nope"],
            turns=[],
            checks_passed=0,
            checks_total=1,
            strict_success=False,
        )

    hold = live_hygiene_block(_state(), _ctx(tmp_path, store, ledger), runner=runner)
    assert hold is not None
    assert hold.outcome is not None
    assert hold.outcome.route == "one_off"
    assert hold.outcome.state.draft is None
    assert "verifier_fail" in (hold.outcome.note or "")
    assert calls == [1]

    import recertia.nodes.distill as distill_mod
    from recertia.nodes.rsi_bridge import HygieneHold

    def boom(*_args, **_kwargs):
        raise AssertionError("distill_success called")

    def blocked(state, ctx, *, runner=None):
        del ctx, runner
        return HygieneHold(outcome=hold.outcome)

    monkeypatch.setattr(distill_mod, "distill_success", boom)
    monkeypatch.setattr("recertia.nodes.rsi_bridge.live_hygiene_block", blocked)
    outcome = _author_or_reject(_state(), _ctx(tmp_path, store, ledger))
    assert outcome.route == "one_off"
    assert outcome.state.draft is None


def test_retrieve_misses_a_screened_artifact(tmp_path: Path) -> None:
    store, ledger = _pair(tmp_path)
    accepted = run_durable_hygiene(
        _req("screen"),
        store=store,
        ledger=ledger,
        executor=_ok(observations=["kept-in-blob"]),
    )
    assert accepted.artifact_id is not None

    class _Search:
        def search(self, *_args, **_kwargs):
            skills = [
                SkillCandidateRef(skill_id=accepted.artifact_id or "scr_missing", version=1, score=1.0),
                SkillCandidateRef(skill_id="real-skill", version=1, score=0.4),
            ]
            return MemoryBundle(skills=skills), type("Exp", (), {"snapshot_id": "snap", "dropped": []})()

        def snapshot_id(self):
            return "snap"

    ctx = _ctx(tmp_path, store, ledger)
    ctx.retriever = _Search()  # type: ignore[assignment]
    outcome = retrieve(_state(), ctx)
    ids = [item.skill_id for item in outcome.state.bundle.skills]
    refs = [item.ref for item in outcome.state.bundle.cases]
    refs.extend(item.ref for item in outcome.state.bundle.dead_ends)
    assert ids == ["real-skill"]
    assert accepted.artifact_id not in refs
    assert all(not item.startswith("scr_") for item in ids + refs)


def test_distill_imported_refuses_without_a_screened_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECERTIA_HYGIENE_ENABLED", raising=False)
    imported = TrajectoryImport.model_validate(_distillable())
    version = distill_imported(imported, SkillStore(tmp_path / "skills"), task_class="bug_reproduction")
    assert version.skill_id == "import-imp-d"
    monkeypatch.setenv("RECERTIA_HYGIENE_ENABLED", "1")
    with pytest.raises(DistillRejected, match="hygiene gate did not accept"):
        distill_imported(imported, SkillStore(tmp_path / "skills2"), task_class="bug_reproduction")


def test_library_flag_on_rejects_before_write_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_HYGIENE_ENABLED", "1")
    store = SkillStore(tmp_path / "skills")

    def executor(_public):
        return ExecResult(
            fresh_execution_id="fx-fail",
            actions=["echo ok"],
            observations=["nope"],
            turns=[],
            checks_passed=0,
            checks_total=1,
            strict_success=False,
        )

    from recertia.errors import ConfigError

    with pytest.raises(ConfigError, match="verifier_fail"):
        LibraryService().distill_trajectory(
            Principal.local_operator(),
            store=store,
            trajectory=_distillable(),
            actor="tester",
            task_class="bug_reproduction",
            runs_root=tmp_path / "runs",
            executor=executor,
        )
    assert list((tmp_path / "skills").rglob("*.json")) == []


def test_library_flag_on_accept_points_at_the_screened_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_HYGIENE_ENABLED", "1")
    store = SkillStore(tmp_path / "skills")
    body = LibraryService().distill_trajectory(
        Principal.local_operator(),
        store=store,
        trajectory=_distillable(),
        actor="tester",
        task_class="bug_reproduction",
        runs_root=tmp_path / "runs",
        executor=_ok(),
    )
    assert body["promoted"] is False
    assert body["lifecycle"] == "candidate"
    version = store.get_version(body["skill_id"], body["version"])
    assert version.provenance.source_case_ids
    assert version.provenance.source_case_ids[0].startswith("scr_")


def test_import_trajectory_does_not_call_the_bridge() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "recertia" / "app" / "library.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LibraryService":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "import_trajectory":
                    text = ast.get_source_segment(source.read_text(encoding="utf-8"), item) or ""
                    assert "rsi_bridge" not in text
                    assert "run_in_container" not in text
                    assert "run_durable_hygiene" not in text
                    return
    raise AssertionError("import_trajectory missing")
