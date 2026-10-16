"""Named regressions for the Phase 0 hotfix train."""

from __future__ import annotations

import inspect
import json
import logging
import multiprocessing
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion, clamp_criterion_timeouts
from contracts.goal import Constraint, DesiredState, Goal, compile_goal
from contracts.policy import JobQuota
from contracts.run import RunState, Task
from recertia.graph.engine import GraphOrchestrator, RunIdExistsError
from recertia.graph.store import CheckpointConflict, CheckpointStore
from recertia.ledger import HashChainLedger
from recertia.memory.affordance import AffordanceStore
from recertia.policy_load import QuotaSidecar, QuotaSidecarCorrupt
from recertia.solver.container import (
    ContainerSpec,
    _container_run,
    _local_run,
    allow_custom_image,
    configured_backend,
    default_container_image,
    execution_backend_override,
)
from recertia.solver.sandbox import SandboxError, SandboxLimits
from recertia.validation.freeze import _freeze_script


def _goal_dir(tmp_path: Path) -> Path:
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "goal.json").write_text(
        json.dumps(
            {
                "desired": [
                    {"id": "f", "kind": "file_exists", "path": "marker.txt", "weight": 1.0}
                ]
            }
        ),
        encoding="utf-8",
    )
    return golden


def test_golden_does_not_mutate_execution_backend_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.1: golden must not write RECERTIA_EXECUTION_BACKEND into the process env."""

    from recertia.evals.golden import run_goal_fixture

    monkeypatch.delenv("RECERTIA_EXECUTION_BACKEND", raising=False)
    assigned: list[str] = []
    environ_cls = type(os.environ)
    original = environ_cls.__setitem__

    def wrapped(self: object, key: str, value: str) -> None:
        if key == "RECERTIA_EXECUTION_BACKEND":
            assigned.append(value)
        original(self, key, value)  # type: ignore[arg-type]

    monkeypatch.setattr(environ_cls, "__setitem__", wrapped)
    try:
        run_goal_fixture(_goal_dir(tmp_path), runs_root=tmp_path / "runs")
    except Exception:
        pass
    assert assigned == []
    assert os.environ.get("RECERTIA_EXECUTION_BACKEND") is None


def test_execution_backend_override_is_not_process_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.1: a local override in one thread must not flip concurrent callers."""

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    seen: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def reader() -> None:
        started.set()
        assert release.wait(timeout=5)
        seen.append(configured_backend())

    thread = threading.Thread(target=reader)
    thread.start()
    assert started.wait(timeout=5)
    with execution_backend_override("local"):
        assert configured_backend() == "local"
        assert os.environ["RECERTIA_EXECUTION_BACKEND"] == "container"
        release.set()
        thread.join(timeout=5)
        assert configured_backend() == "local"
    assert seen == ["container"]
    assert configured_backend() == "container"


def test_cli_prepare_does_not_assign_backend_env() -> None:
    source = Path("src/recertia/cli/runs.py").read_text(encoding="utf-8")
    golden = Path("src/recertia/evals/golden.py").read_text(encoding="utf-8")
    assert 'os.environ["RECERTIA_EXECUTION_BACKEND"]' not in source
    assert 'os.environ["RECERTIA_EXECUTION_BACKEND"]' not in golden


def test_file_exists_rejects_shell_metacharacters_and_keeps_safe_paths() -> None:
    """0.2: file_exists/file_contains must not emit unquoted shell."""

    safe = compile_goal(
        Goal(desired=[DesiredState(id="ec", kind="file_exists", path=".editorconfig")])
    )
    assert safe[0].kind == "assertion"
    assert safe[0].expr == "file_exists('.editorconfig')"
    assert safe[0].run is None

    contains = compile_goal(
        Goal(
            desired=[
                DesiredState(
                    id="py",
                    kind="file_contains",
                    path=".editorconfig",
                    pattern=r"\[\*\.py\]",
                )
            ]
        )
    )
    assert contains[0].kind == "assertion"
    assert contains[0].expr is not None
    assert contains[0].expr.startswith("file_contains(")
    assert ".editorconfig" in contains[0].expr
    assert contains[0].run is None
    assert "';" not in contains[0].expr

    for bad in ("foo; touch pwned", "/etc/passwd", "../etc/passwd", "foo/../../etc/passwd", "a b"):
        with pytest.raises(ValueError):
            compile_goal(Goal(desired=[DesiredState(id="f", kind="file_exists", path=bad)]))
        with pytest.raises(ValueError):
            compile_goal(
                Goal(
                    desired=[
                        DesiredState(id="c", kind="file_contains", path=bad, pattern="x")
                    ]
                )
            )


def test_must_not_modify_placeholder_quotes_paths() -> None:
    criteria = compile_goal(
        Goal(
            desired=[DesiredState(id="ok", kind="file_exists", path="frozen.txt")],
            constraints=[Constraint(id="fz", kind="must_not_modify", value=["frozen.txt"])],
        )
    )
    freeze = next(c for c in criteria if c.id == "fz")
    assert freeze.run == "test -e frozen.txt"
    with pytest.raises(ValueError):
        compile_goal(
            Goal(
                desired=[DesiredState(id="ok", kind="file_exists", path="x")],
                constraints=[Constraint(id="fz", kind="must_not_modify", value=["x; id"])],
            )
        )


def test_freeze_script_runs_under_sh(tmp_path: Path) -> None:
    """0.2: the sealed check must be a valid sh script, not a Python repr."""

    (tmp_path / "frozen.txt").write_text("v1", encoding="utf-8")
    from recertia.validation.freeze import path_digest

    script = _freeze_script({"frozen.txt": path_digest(tmp_path, "frozen.txt")})
    assert script.startswith("python -c ")
    host = "python3 -c " + script[len("python -c ") :]
    proc = subprocess.run(
        ["sh", "-c", host],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr

    quoted = _freeze_script({"it's.txt": "abc"})
    host_quote = "python3 -c " + quoted[len("python -c ") :]
    proc_quote = subprocess.run(
        ["sh", "-c", host_quote],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc_quote.returncode in {0, 1}
    assert "SyntaxError" not in (proc_quote.stderr or "")


def test_timeout_clamped_after_compile_goal(tmp_path: Path) -> None:
    """0.2: DesiredState.timeout_s above the API ceiling does not survive intake."""

    workdir = tmp_path / "wd"
    workdir.mkdir()
    goal = Goal(
        desired=[DesiredState(id="out", kind="file_exists", path="out.txt", timeout_s=3600)]
    )
    loose = compile_goal(goal)
    assert loose[0].timeout_s == 3600
    clamped = clamp_criterion_timeouts(loose, ceiling_s=300)
    assert clamped[0].timeout_s == 300

    orch = GraphOrchestrator(tmp_path / "runs", max_criterion_timeout_s=300)
    try:
        state = orch.start(
            "clamp-1",
            Task(
                task_id="clamp-1",
                goal=goal,
                submitted_at=datetime.now(timezone.utc),
            ),
            [],
            workdir=workdir,
            budget=__import__("contracts.budget", fromlist=["Budget"]).Budget(max_attempts=1),
            max_steps=1,
        )
    finally:
        orch.close()
    assert state.criteria
    assert state.criteria[0].timeout_s == 300
    assert state.criteria[0].timeout_s <= 300


def test_session_rejects_path_tenant_ids() -> None:
    """0.3: ../../../outside_root cannot be a session tenant."""

    from recertia.api.console_auth import ConsoleUser, SessionStore

    store = SessionStore(secret="k" * 32)
    bad = ConsoleUser(
        user_id="u",
        display_name="u",
        roles=frozenset({"operator"}),
        tenants=("../../../outside_root",),
        active_tenant="../../../outside_root",
    )
    with pytest.raises(ValueError, match="tenant_id"):
        store.issue(bad)

    outsider = ConsoleUser(
        user_id="u",
        display_name="u",
        roles=frozenset({"operator"}),
        tenants=("default",),
        active_tenant="../../../outside_root",
    )
    with pytest.raises(ValueError, match="active_tenant|tenant_id"):
        store.issue(outsider)

    good = store.issue(
        ConsoleUser(
            user_id="u",
            display_name="u",
            roles=frozenset({"operator"}),
            tenants=("default",),
            active_tenant="default",
        )
    )
    assert store.parse(good) is not None

    import hashlib
    import hmac

    payload = {
        "user_id": "u",
        "display_name": "u",
        "roles": ["operator"],
        "tenants": ["../../../outside_root"],
        "active_tenant": "../../../outside_root",
        "exp": 4_000_000_000,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(store._hmac_key, body.encode(), hashlib.sha256).hexdigest()
    assert store.parse(f"{body}.{sig}") is None


def test_dev_login_rejects_traversal_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "dev")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "t" * 32)
    monkeypatch.setenv("RECERTIA_CONSOLE_COOKIE_SECURE", "0")
    app = create_app(root=tmp_path / "api-root")
    client = TestClient(app)
    denied = client.post(
        "/v1/auth/dev-login",
        json={"user_id": "x", "roles": ["operator"], "tenants": ["../../../outside_root"]},
    )
    assert denied.status_code == 400


def _run_record(tenant: str, run_id: str):
    from recertia.api import RunRecord

    return RunRecord(
        run_id=run_id,
        request="x",
        task_class="repo-chore",
        status="running",
        created_at=datetime.now(timezone.utc),
        tenant_id=tenant,
    )


def test_events_and_cancel_are_tenant_owned(tmp_path: Path) -> None:
    """0.4: another tenant's GET events and POST cancel are 404."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    app = create_app(root=tmp_path / "api-root")
    key_a = app.state.api_keys.issue(tenant_id="tenant-a", scopes={"runs"}, actor="test")
    key_b = app.state.api_keys.issue(tenant_id="tenant-b", scopes={"runs"}, actor="test")
    app.state.runs[("tenant-a", "shared-id")] = _run_record("tenant-a", "shared-id")
    app.state.console_ctx.events.append("tenant-a", "shared-id", "run.queued", {"n": 1})
    client = TestClient(app)
    headers_a = {"X-API-Key": key_a.secret}
    headers_b = {"X-API-Key": key_b.secret}

    own = client.get("/v1/runs/shared-id/events", headers=headers_a)
    assert own.status_code == 200
    assert "run.queued" in own.text

    cross_get = client.get("/v1/runs/shared-id/events", headers=headers_b)
    assert cross_get.status_code == 404
    cross_cancel = client.post("/v1/runs/shared-id/cancel", headers=headers_b)
    assert cross_cancel.status_code == 404
    worker = app.state.console_ctx.worker
    assert worker.is_cancelled("tenant-b", "shared-id") is False
    assert worker.is_cancelled("tenant-a", "shared-id") is False

    ok = client.post("/v1/runs/shared-id/cancel", headers=headers_a)
    assert ok.status_code == 200
    assert worker.is_cancelled("tenant-a", "shared-id") is True
    assert worker.is_cancelled("tenant-b", "shared-id") is False
    event_file = (
        tmp_path / "api-root" / "runs" / "tenant-a" / "run_events" / "shared-id.jsonl"
    )
    assert event_file.is_file()
    assert not (
        tmp_path / "api-root" / "runs" / "tenant-b" / "run_events" / "shared-id.jsonl"
    ).exists()


def test_review_alias_enforces_t2_gate(tmp_path: Path) -> None:
    """0.5: POST /v1/reviews/{id} matches the proposal T2 reviewer-or-admin check."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app
    from recertia.proposals.store import ProposalRecord

    app = create_app(root=tmp_path / "api-root")
    runs_only = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    admin = app.state.api_keys.issue(tenant_id="t1", scopes={"runs", "admin"}, actor="test")
    app.state.console_ctx.proposals.add(
        ProposalRecord(
            proposal_id="rev-t2",
            kind="policy",
            skill_id="policy",
            version=0,
            rationale="needs a reviewer",
            payload={"tier": "T2"},
            tenant_id="t1",
        )
    )
    client = TestClient(app)
    denied = client.post(
        "/v1/reviews/rev-t2",
        headers={"X-API-Key": runs_only.secret},
        json={"decision": "approve", "note": "nope"},
    )
    assert denied.status_code == 403
    approved = client.post(
        "/v1/reviews/rev-t2",
        headers={"X-API-Key": admin.secret},
        json={"decision": "approve", "note": "ok"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


def test_resume_admits_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.5: resume calls quota admit and can 429 when the tenant is at the cap."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs", "exec"}, actor="test")
    client = TestClient(app)
    headers = {"X-API-Key": issued.secret}
    created = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "request": "resume me",
            "run_id": "resume-q",
            "script": ["true"],
            "budget": {"max_attempts": 1},
            "criteria": [
                {
                    "id": "ok",
                    "kind": "command",
                    "run": "true",
                    "source": "caller",
                    "weight": 1.0,
                }
            ],
        },
    )
    assert created.status_code == 200, created.text
    store = app.state.quota_store
    with store._lock, store._conn:
        store._conn.execute(
            """
            INSERT INTO inflight(tenant_id, count) VALUES ('t1', ?)
            ON CONFLICT(tenant_id) DO UPDATE SET count = excluded.count
            """,
            (store.defaults.max_in_flight,),
        )
    blocked = client.post("/v1/runs/resume-q/resume", headers=headers)
    assert blocked.status_code == 429


def test_runs_scope_may_submit_command_criteria_without_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.2 / D2: command criteria are not the raw script field; exec is not required."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    created = client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={
            "goal": {
                "desired": [{"id": "c", "kind": "command", "run": "true", "weight": 1.0}],
                "context": "command criterion",
            },
            "run_id": "cmd-no-exec",
            "budget": {"max_attempts": 1},
        },
    )
    assert created.status_code != 403
    assert created.status_code == 200, created.text


def test_memory_query_rejects_host_workdir(tmp_path: Path) -> None:
    """0.6: absolute and escaping workdirs are rejected and not created."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    headers = {"X-API-Key": issued.secret}
    absolute = tmp_path / "should-not-exist"
    denied = client.post(
        "/v1/memory/query",
        headers=headers,
        json={"query": "hello", "workdir": str(absolute)},
    )
    assert denied.status_code == 400
    assert not absolute.exists()

    escape = client.post(
        "/v1/memory/query",
        headers=headers,
        json={"query": "hello", "workdir": "../../../../tmp/recertia-phase0-escape"},
    )
    assert escape.status_code == 400
    assert not Path("/tmp/recertia-phase0-escape").exists()

    ok = client.post(
        "/v1/memory/query",
        headers=headers,
        json={"query": "hello", "workdir": "nested"},
    )
    assert ok.status_code == 200, ok.text
    confined = tmp_path / "api-root" / "runs" / "t1" / "memory-query" / "nested"
    assert confined.is_dir()


def test_local_home_is_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.7: the child HOME is the workdir, not the host home."""

    monkeypatch.setenv("HOME", "/tmp/host-home-should-not-leak")
    limits = SandboxLimits(allowed_env_keys=("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"))
    proc = _local_run(
        'python3 -c \'import os; print(os.environ["HOME"])\'',
        workdir=tmp_path,
        limits=limits,
        timeout_s=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(tmp_path.resolve())
    assert "HOME" not in SandboxLimits().allowed_env_keys


def test_setrlimit_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.7: when the platform supports setrlimit, a failure does not run the command."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("rlimit denied")

    monkeypatch.setattr("resource.setrlimit", boom)
    with pytest.raises(SandboxError, match="setrlimit"):
        _local_run("echo should-not-run", workdir=tmp_path, limits=SandboxLimits(), timeout_s=10)


def test_container_invocation_sets_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("recertia.solver.container.subprocess.run", fake_run)
    _container_run(
        "docker", "true", workdir=tmp_path, spec=ContainerSpec(), timeout_s=5
    )
    assert captured
    args = captured[0]
    assert "HOME=/work" in args


def test_allow_custom_image_is_explicit_not_any_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.8: only 1/true/yes/on opt in. ``0`` is false."""

    monkeypatch.setenv("RECERTIA_CONTAINER_IMAGE", "evil:latest")
    for raw in ("0", "false", "no", "off", "2", "yesplease"):
        monkeypatch.setenv("RECERTIA_ALLOW_CUSTOM_IMAGE", raw)
        assert allow_custom_image() is False
        with pytest.raises(SandboxError):
            default_container_image()
    for raw in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("RECERTIA_ALLOW_CUSTOM_IMAGE", raw)
        assert allow_custom_image() is True
        assert default_container_image() == "evil:latest"


def _append_ledger(path: str, count: int, actor: str) -> None:
    ledger = HashChainLedger(path)
    for index in range(count):
        ledger.append(actor=actor, action="write", target=f"{actor}-{index}")


def test_ledger_append_flock_keeps_one_chain(tmp_path: Path) -> None:
    """0.9: concurrent processes cannot fork the hash chain."""

    path = tmp_path / "ledger.jsonl"
    procs = [
        multiprocessing.Process(target=_append_ledger, args=(str(path), 12, f"p{i}"))
        for i in range(4)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(30)
        assert proc.exitcode == 0
    ledger = HashChainLedger(path)
    ledger.verify()
    assert len(ledger.entries()) == 48


def test_quota_sidecar_corrupt_fails_closed_and_keeps_computer_use(tmp_path: Path) -> None:
    """0.10: JSONDecodeError must not look like zero spend."""

    broken = tmp_path / "job_quota.json"
    broken.write_text("{", encoding="utf-8")
    sidecar = QuotaSidecar(broken)
    with pytest.raises(QuotaSidecarCorrupt):
        sidecar.merge(JobQuota(weekly_token_cap=1000))
    assert broken.read_text(encoding="utf-8") == "{"

    path = tmp_path / "ok.json"
    spent = JobQuota(
        weekly_token_cap=1000,
        tokens_spent=40,
        computer_use_tokens_spent=15,
        hex_tokens_spent=5,
    )
    QuotaSidecar(path).save(spent)
    merged = QuotaSidecar(path).merge(JobQuota(weekly_token_cap=1000))
    assert merged.tokens_spent == 40
    assert merged.computer_use_tokens_spent == 15
    assert merged.hex_tokens_spent == 5
    assert path.is_file()
    json.loads(path.read_text(encoding="utf-8"))


def test_affordance_store_quarantines_corrupt_json(tmp_path: Path) -> None:
    """0.11: a torn affordances file is renamed aside and bootstrap starts empty."""

    path = tmp_path / "affordances.json"
    path.write_text("{not-json", encoding="utf-8")
    store = AffordanceStore(path)
    assert store.tools == {}
    assert not path.exists()
    quarantined = list(tmp_path.glob("affordances.json.corrupt-*"))
    assert len(quarantined) == 1
    store.save()
    assert json.loads(path.read_text(encoding="utf-8"))["tools"] == {}


def _state(run_id: str) -> RunState:
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="x",
            submitted_at=datetime.now(timezone.utc),
        ),
    )


def test_checkpoint_insert_rejects_duplicate_seq(tmp_path: Path) -> None:
    """0.12: duplicate (run_id, seq) is loud."""

    store = CheckpointStore(tmp_path / "checkpoints.db")
    try:
        state = _state("r")
        store.save("r", 0, "intake", "solve", state)
        with pytest.raises(CheckpointConflict):
            store.save("r", 0, "intake", "solve", state)
    finally:
        store.close()


def test_start_rejects_run_id_with_existing_checkpoints(tmp_path: Path) -> None:
    workdir = tmp_path / "wd"
    workdir.mkdir()
    runs = tmp_path / "runs"
    first = GraphOrchestrator(runs)
    try:
        first.start(
            "again",
            Task(task_id="again", request="x", submitted_at=datetime.now(timezone.utc)),
            [TaskCriterion(id="ok", kind="command", run="true", source="caller", weight=1.0)],
            workdir=workdir,
            max_steps=1,
        )
    finally:
        first.close()
    second = GraphOrchestrator(runs)
    try:
        with pytest.raises(RunIdExistsError):
            second.start(
                "again",
                Task(task_id="again", request="x", submitted_at=datetime.now(timezone.utc)),
                [TaskCriterion(id="ok", kind="command", run="true", source="caller")],
                workdir=workdir,
            )
    finally:
        second.close()


def test_create_run_409_when_checkpoints_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    root = tmp_path / "api-root"
    app = create_app(root=root)
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    db = root / "runs" / "t1" / "checkpoints.db"
    db.parent.mkdir(parents=True)
    store = CheckpointStore(db)
    try:
        store.save("reuse-1", 0, "intake", None, _state("reuse-1"))
    finally:
        store.close()
    client = TestClient(create_app(root=root))
    # New app: in-memory runs dict is empty; the checkpoint row is still there.
    fresh = client.post(
        "/v1/runs",
        headers={"X-API-Key": issued.secret},
        json={"request": "again", "run_id": "reuse-1", "budget": {"max_attempts": 1}},
    )
    assert fresh.status_code == 409


def test_startup_resets_leaked_inflight(tmp_path: Path) -> None:
    """0.13: a crashed admit must not 429 the tenant after restart."""

    pytest.importorskip("fastapi")

    from recertia.api import create_app
    from recertia.api.quotas import QuotaStore

    root = tmp_path / "api-root"
    root.mkdir()
    leaked = QuotaStore(root / "quotas.sqlite")
    leaked.admit("t1")
    with leaked._lock, leaked._conn:
        leaked._conn.execute("UPDATE inflight SET count = 99 WHERE tenant_id = 't1'")
    leaked.close()
    app = create_app(root=root)
    assert app.state.quota_store.snapshot("t1")["in_flight"] == 0
    app.state.quota_store.admit("t1")


def test_create_run_logs_internal_error_without_leaking_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """0.14: 500 paths log the exception and return a generic body."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app
    from recertia.graph.engine import GraphOrchestrator

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")

    def boom(self: object, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise RuntimeError("boom-marker-not-a-secret")

    monkeypatch.setattr(GraphOrchestrator, "start", boom)
    app = create_app(root=tmp_path / "api-root")
    issued = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test")
    client = TestClient(app)
    with caplog.at_level(logging.ERROR, logger="recertia.api"):
        resp = client.post(
            "/v1/runs",
            headers={"X-API-Key": issued.secret},
            json={"request": "explode", "run_id": "boom-1", "budget": {"max_attempts": 1}},
        )
    assert resp.status_code == 500
    assert "boom-marker-not-a-secret" not in resp.text
    assert "internal" in resp.text
    assert "boom-marker-not-a-secret" in caplog.text
    assert issued.secret not in caplog.text


def test_health_is_async(tmp_path: Path) -> None:
    """0.15: GET /health must not occupy an AnyIO worker thread."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    app = create_app(root=tmp_path / "api-root")
    route = next(r for r in app.routes if getattr(r, "path", None) == "/health")
    assert inspect.iscoroutinefunction(route.endpoint)
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_agents_documents_tenant_job_quota_path() -> None:
    """0.16: AGENTS.md names the sidecar the job runner actually writes."""

    text = Path("AGENTS.md").read_text(encoding="utf-8")
    assert "jobs/job_quota.json" in text
    assert ".recertia/runs/<tenant>/jobs/job_quota.json" in text
    assert "spend is `.recertia/job_quota.json`" not in text


def test_checkpoint_sql_is_insert_not_replace() -> None:
    source = Path("src/recertia/graph/store.py").read_text(encoding="utf-8")
    assert "INSERT OR REPLACE INTO checkpoints" not in source
    assert "INSERT INTO checkpoints" in source
