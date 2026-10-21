"""REC-009 (Gap A): a promotion refusal never shows proposers a failing fixture id.

The skill and the fixture have different names (skill ``leak-probe``, failing fixture
dir ``HELDOUT-x``), so "absent" means ``HELDOUT-x`` and its golden path occur nowhere in
proposer-visible output, with no carve-out. The skill id may appear: the caller supplied
it. The operator audit record, read by an operator, does hold the id.

Proposer-visible outputs checked: ``str(exc)``, ``repr(exc)``, every exception attribute,
the promotion log written for the refused version, the promote HTTP body,
``GET /v1/jobs/{id}`` and ``GET /v1/jobs`` for a runs-only API key, CLI stderr without
``--operator-detail``, and every file under the gate's ``runs_root``. All fixtures are
synthetic and live under ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from recertia.evals.golden import GoldenReport, GoldenResult
from recertia.memory.procedural.store import SkillStore
from recertia.review.promotion import PromotionError, promote_to_approved
from recertia.review.promotion_audit import OperatorAuditStore
from tests.support.http import error_text
from tests.support.promotion_fixtures import ABS_PATH, FIXTURE, SKILL
from tests.support.promotion_fixtures import assert_no_leak as _assert_no_leak
from tests.support.promotion_fixtures import exception_surfaces as _exception_surfaces
from tests.support.promotion_fixtures import leak_golden as _golden
from tests.support.promotion_fixtures import leak_skill as _skill

KINDS = ("golden_gate", "predecessor_non_regression")


def _refuse(kind: str, tmp_path: Path, store: SkillStore, *, log_dir: Path, audit: Any) -> int:
    """Plant ``HELDOUT-x`` and force a refusal of ``kind``; return the refused version."""

    golden = tmp_path / "golden" / "repo-chore"
    runs = tmp_path / "runs"
    if kind == "golden_gate":
        store.write_candidate(_skill(1, command="true", criterion_run="test -f MISSING.txt"))
        _golden(golden / FIXTURE, command="test -f MISSING.txt")
        target, golden_dir = 1, golden / FIXTURE
    else:
        store.write_candidate(
            _skill(
                1,
                command="python3 -c \"open('MUST_EXIST.txt','w').write('ok')\"",
                criterion_run="test -f MUST_EXIST.txt",
            )
        )
        first = promote_to_approved(
            store,
            SKILL,
            1,
            golden_dir=_golden(golden / FIXTURE, command="test -f MUST_EXIST.txt"),
            runs_root=runs,
            log_dir=log_dir,
            audit=audit,
        )
        assert first.lifecycle == "approved"
        store.write_candidate(_skill(2, command="true", criterion_run="test -f OTHER.txt"))
        _golden(golden / "own-fixture", command="test -f OTHER.txt", workspace_file="OTHER.txt")
        target, golden_dir = 2, golden / "own-fixture"
    with pytest.raises(PromotionError) as caught:
        promote_to_approved(
            store,
            SKILL,
            target,
            golden_dir=golden_dir,
            runs_root=runs,
            log_dir=log_dir,
            audit=audit,
        )
    pytest.refused = caught.value  # type: ignore[attr-defined]
    return target


@pytest.mark.parametrize("kind", KINDS)
def test_promotion_refusal_omits_fixture_ids(kind: str, tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    log_dir = tmp_path / "evals" / "golden" / "_promotion_logs"
    audit = OperatorAuditStore(tmp_path / "operator", forbidden_roots=[tmp_path / "runs"])
    version = _refuse(kind, tmp_path, store, log_dir=log_dir, audit=audit)
    exc: PromotionError = pytest.refused  # type: ignore[attr-defined]
    fixture_dir = tmp_path / "golden" / "repo-chore" / FIXTURE

    # 1-3: exception text and every attribute.
    for surface in _exception_surfaces(exc):
        _assert_no_leak(surface, fixture_dir, tmp_path)
    assert exc.refusal_kind == kind
    assert exc.failing_count == 1
    assert SKILL in str(exc)

    # 8: the promotion log written for the refused version is decision-only.
    log_text = (log_dir / f"{SKILL}-v{version}.json").read_text(encoding="utf-8")
    _assert_no_leak(log_text, fixture_dir, tmp_path)
    assert json.loads(log_text)["decision"] == "refused"

    # 8-10: nothing under the gate's runs_root (trajectories, tape, checkpoints, golden
    # workspaces) names the fixture or its path.
    for path in (tmp_path / "runs").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert FIXTURE.encode() not in data, path.name
            assert str(fixture_dir).encode() not in data, path.name

    # Operator audit record, read by an operator, names the fixture and its path.
    assert exc.audit_ref is not None
    record = audit.read(exc.audit_ref)
    assert record is not None
    assert record["failing_fixtures"] == [FIXTURE]
    assert record["refusal_kind"] == kind
    if kind == "golden_gate":
        assert any(row["golden_path"] == str(fixture_dir) for row in record["results"])
    else:
        assert FIXTURE in record["detail"]
    assert store.get_status(SKILL, version).lifecycle == "candidate"


class _SpyGate:
    """GateRunner stand-in that records whether the golden gate ran."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, version: Any, *, runs_root: Path, **kwargs: Any) -> GoldenReport:
        del version, runs_root, kwargs
        self.calls += 1
        return GoldenReport()


def test_promotion_without_an_audit_sink_refuses_before_the_gate(tmp_path: Path) -> None:
    """M1: no sink means no gate run, no log, no lifecycle change (fail closed)."""

    store = SkillStore(tmp_path / "skills")
    store.write_candidate(_skill(1, command="true", criterion_run="true"))
    gate = _SpyGate()
    log_dir = tmp_path / "logs"
    with pytest.raises(PromotionError) as caught:
        promote_to_approved(
            store,
            SKILL,
            1,
            golden_dir=tmp_path / "golden" / "repo-chore" / FIXTURE,
            runs_root=tmp_path / "runs",
            log_dir=log_dir,
            audit=None,
            gate=gate,
        )
    exc = caught.value
    assert exc.refusal_kind == "audit_unavailable"
    assert exc.audit_ref is None and exc.failing_count == 0
    assert "the golden gate did not run" in str(exc)
    assert gate.calls == 0
    assert not log_dir.exists() and not (tmp_path / "runs").exists()
    assert store.get_status(SKILL, 1).lifecycle == "candidate"
    for surface in _exception_surfaces(exc):
        _assert_no_leak(surface, tmp_path)


class _BrokenAuditStore(OperatorAuditStore):
    """A real store whose write fails mid-way, after the gate already ran."""

    def record(self, entry: Any) -> str:
        del entry
        raise sqlite3.OperationalError(f"disk I/O error under {self.root}/{FIXTURE}")


def test_audit_write_failure_after_the_gate_refuses_as_audit_unavailable(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    log_dir = tmp_path / "logs"
    audit = _BrokenAuditStore(tmp_path / "operator")
    store.write_candidate(_skill(1, command="true", criterion_run="test -f MISSING.txt"))
    fixture = _golden(tmp_path / "golden" / "repo-chore" / FIXTURE, command="test -f MISSING.txt")
    with pytest.raises(PromotionError) as caught:
        promote_to_approved(
            store,
            SKILL,
            1,
            golden_dir=fixture,
            runs_root=tmp_path / "runs",
            log_dir=log_dir,
            audit=audit,
        )
    exc = caught.value
    assert exc.refusal_kind == "audit_unavailable"
    assert "the refusal detail was not recorded" in str(exc)
    for surface in _exception_surfaces(exc):  # also: no chained sqlite3 error
        _assert_no_leak(surface, fixture, tmp_path)
    assert not (log_dir / f"{SKILL}-v1.json").exists()
    assert store.get_status(SKILL, 1).lifecycle == "candidate"


GATE_ERRORS = ("oserror_names_the_path", "validation_error_echoes_the_input")


@pytest.mark.parametrize("case", GATE_ERRORS)
def test_gate_error_detail_goes_to_the_audit_sink_only(case: str, tmp_path: Path) -> None:
    """M5: the gate error's own text names the fixture and its path; proposers never see it.

    The audit record must contain the fixture id and path, which proves the planted
    error really echoes them (a vacuous error would let a re-appended message pass).
    """

    store = SkillStore(tmp_path / "skills")
    store.write_candidate(_skill(1, command="true", criterion_run="true"))
    fixture = tmp_path / "golden" / "repo-chore" / FIXTURE
    fixture.mkdir(parents=True)
    if case == "oserror_names_the_path":
        (fixture / "task.json").mkdir()  # reading it raises IsADirectoryError(path)
    else:
        (fixture / "task.json").write_text(
            json.dumps({"request": "synthetic", "expected_skill_id": SKILL}) + "\n",
            encoding="utf-8",
        )
        (fixture / "goal.json").write_text(
            json.dumps({"context": [f"{FIXTURE} lives at {fixture}"]}) + "\n",
            encoding="utf-8",
        )
    audit = OperatorAuditStore(tmp_path / "operator")
    with pytest.raises(PromotionError) as caught:
        promote_to_approved(
            store,
            SKILL,
            1,
            golden_dir=fixture,
            runs_root=tmp_path / "runs",
            log_dir=tmp_path / "logs",
            audit=audit,
        )
    exc = caught.value
    assert exc.refusal_kind == "gate_error"
    for surface in _exception_surfaces(exc):
        _assert_no_leak(surface, fixture, tmp_path)
    _assert_no_leak((tmp_path / "logs" / f"{SKILL}-v1.json").read_text(encoding="utf-8"), tmp_path)
    record = audit.read(exc.audit_ref or "")
    assert record is not None
    expected = "IsADirectoryError" if case == "oserror_names_the_path" else "ValidationError"
    assert record["detail"].startswith(expected), record["detail"]
    assert FIXTURE in record["detail"]
    # pydantic shortens long input values; the OSError carries the full path.
    if case == "oserror_names_the_path":
        assert str(fixture) in record["detail"]
    else:
        assert ABS_PATH.search(record["detail"]) and "repo-chore/" in record["detail"]


# ----- HTTP: promote body and the jobs endpoints for a runs-only key -----


class _FailingGate:
    """GateRunner stand-in: the planted fixture fails, everything else passes."""

    def __init__(self, fixture_dir: Path, *, fail_planted: bool) -> None:
        self.fixture_dir = fixture_dir
        self.fail_planted = fail_planted

    def run(self, version: Any, *, runs_root: Path, **kwargs: Any) -> GoldenReport:
        del runs_root
        planted = GoldenResult(
            skill_id=version.skill_id,
            version=version.version,
            golden_path=str(self.fixture_dir),
            passed=False,
            terminal="failed",
            run_id=f"golden-{version.skill_id}-v{version.version}-abc123",
            detail=f"criterion failed in {self.fixture_dir}",
        )
        if self.fail_planted:
            return GoldenReport(results=[planted])
        own = GoldenResult(
            skill_id=version.skill_id,
            version=version.version,
            golden_path=str(self.fixture_dir.parent / "own-fixture"),
            passed=True,
            terminal="solved",
            run_id="golden-own",
        )
        return GoldenReport(results=[own])


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_OPERATOR_AUDIT_ROOT", str(tmp_path / "operator"))
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    return TestClient(app), app


def _key(app: Any, scopes: set[str]) -> dict[str, str]:
    issued = app.state.api_keys.issue(tenant_id="t1", scopes=scopes, actor="test")
    return {"X-API-Key": issued.secret}


@pytest.mark.parametrize("kind", KINDS)
def test_runs_only_key_sees_no_fixture_ids_from_jobs_endpoints(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _app(tmp_path, monkeypatch)
    ctx = app.state.recertia
    fixture_dir = tmp_path / "heldout-src" / "repo-chore" / FIXTURE
    store = SkillStore(ctx.tenant_skills_root("t1"))
    if kind == "golden_gate":
        store.write_candidate(_skill(1, command="true", criterion_run="true"))
        version = 1
    else:
        store.write_candidate(
            _skill(
                1,
                command="python3 -c \"open('MUST_EXIST.txt','w').write('ok')\"",
                criterion_run="test -f MUST_EXIST.txt",
            )
        )
        promote_to_approved(
            store,
            SKILL,
            1,
            golden_dir=_golden(fixture_dir, command="test -f MUST_EXIST.txt"),
            runs_root=tmp_path / "pre-runs",
            log_dir=ctx.tenant_runs_root("t1") / "promotion_logs",
            audit=OperatorAuditStore(tmp_path / "operator"),
        )
        store.write_candidate(_skill(2, command="true", criterion_run="true"))
        version = 2
    monkeypatch.setattr(
        ctx.promotion_service,
        "_gate",
        _FailingGate(fixture_dir, fail_planted=kind == "golden_gate"),
    )

    # 4: the promote body (reviewer-level key).
    resp = client.post(
        f"/v1/skills/{SKILL}/versions/{version}/promote",
        headers=_key(app, {"runs", "promote"}),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["refusal_kind"] == kind
    assert body["failing_count"] == 1
    _assert_no_leak(resp.text, fixture_dir, tmp_path)

    # 5 and 6: a runs-only key on both jobs endpoints.
    runs_only = _key(app, {"runs"})
    one = client.get(f"/v1/jobs/{body['job_run_id']}", headers=runs_only)
    many = client.get("/v1/jobs", headers=runs_only)
    assert one.status_code == 200 and many.status_code == 200
    assert one.json()["status"] == "failed"
    assert body["job_run_id"] in [item["job_run_id"] for item in many.json()["items"]]
    for resp_text in (one.text, many.text):
        _assert_no_leak(resp_text, fixture_dir, tmp_path)
        assert "failing_fixtures" not in resp_text

    # The operator record holds the id; it lives outside the API root.
    record = OperatorAuditStore(tmp_path / "operator").read(body["audit_ref"])
    assert record is not None and record["failing_fixtures"] == [FIXTURE]
    assert not str(OperatorAuditStore(tmp_path / "operator").path).startswith(
        str(tmp_path / "api-root")
    )


def test_jobs_endpoints_redact_legacy_promote_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records written before REC-009 still hold ids; the run-scope view redacts them."""

    client, app = _app(tmp_path, monkeypatch)
    jobs = app.state.recertia.job_runs
    legacy = jobs.create("promote", tenant_id="t1", meta={"skill_id": SKILL, "version": 1})
    legacy.status = "failed"
    legacy.error = (
        f"golden gate failed for {SKILL}@v1: failing fixtures=['{FIXTURE}'] "
        f"(log: {tmp_path}/promotion_logs/{SKILL}-v1.json)"
    )
    legacy.meta["failing_fixtures"] = [FIXTURE]
    jobs.save(legacy)
    runs_only = _key(app, {"runs"})
    for resp in (
        client.get(f"/v1/jobs/{legacy.job_run_id}", headers=runs_only),
        client.get("/v1/jobs", headers=runs_only),
    ):
        assert resp.status_code == 200
        _assert_no_leak(resp.text, tmp_path)
        assert "failing_fixtures" not in resp.text


class _ExplodingGate:
    """GateRunner stand-in whose unexpected error text names the fixture and its path."""

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir

    def run(self, version: Any, *, runs_root: Path, **kwargs: Any) -> GoldenReport:
        del version, runs_root, kwargs
        raise RuntimeError(f"fixture {FIXTURE} blew up reading {self.fixture_dir}/task.json")


def test_promote_route_internal_error_text_never_reaches_proposers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: the generic 400 path keeps the raw exception text in the audit store only."""

    client, app = _app(tmp_path, monkeypatch)
    ctx = app.state.recertia
    SkillStore(ctx.tenant_skills_root("t1")).write_candidate(
        _skill(1, command="true", criterion_run="true")
    )
    fixture_dir = tmp_path / "heldout-src" / "repo-chore" / FIXTURE
    monkeypatch.setattr(ctx.promotion_service, "_gate", _ExplodingGate(fixture_dir))
    resp = client.post(
        f"/v1/skills/{SKILL}/versions/1/promote", headers=_key(app, {"runs", "promote"})
    )
    assert resp.status_code == 400, resp.text
    message = error_text(resp)
    assert message.startswith("promotion failed: internal error; operator detail: audit_ref=pa-")
    runs_only = _key(app, {"runs"})
    jobs = client.get("/v1/jobs", headers=runs_only)
    (job,) = [item for item in jobs.json()["items"] if item.get("job") == "promote"]
    one = client.get(f"/v1/jobs/{job['job_run_id']}", headers=runs_only)
    assert one.json()["status"] == "failed"
    stored = ctx.job_runs.get(job["job_run_id"], tenant_id="t1")
    assert stored is not None
    for text in (resp.text, jobs.text, one.text, json.dumps(stored.to_dict(), default=str)):
        _assert_no_leak(text, fixture_dir, tmp_path)
        assert "blew up" not in text
    record = OperatorAuditStore(tmp_path / "operator").read(message.rsplit("=", 1)[1])
    assert record is not None and record["refusal_kind"] == "internal_error"
    assert FIXTURE in record["detail"] and str(fixture_dir) in record["detail"]


def test_promote_route_returns_404_for_a_missing_skill_or_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q3/S3: unknown skill or version -> 404 with a fixed message, no path, no job."""

    client, app = _app(tmp_path, monkeypatch)
    ctx = app.state.recertia
    SkillStore(ctx.tenant_skills_root("t1")).write_candidate(
        _skill(1, command="true", criterion_run="true")
    )
    gate = _SpyGate()
    monkeypatch.setattr(ctx.promotion_service, "_gate", gate)
    headers = _key(app, {"runs", "promote"})
    for path in (f"/v1/skills/{SKILL}/versions/7/promote", "/v1/skills/no-such-skill/versions/1/promote"):
        resp = client.post(path, headers=headers)
        assert resp.status_code == 404, resp.text
        assert error_text(resp) == "skill version not found"
        _assert_no_leak(resp.text, tmp_path)
    assert gate.calls == 0
    jobs = client.get("/v1/jobs", headers=_key(app, {"runs"})).json()["items"]
    assert not [item for item in jobs if item.get("job") == "promote"]


AUDIT_CASES = ("unset", "inside_api_root", "inside_runs_root", "open_error")


@pytest.mark.parametrize("case", AUDIT_CASES)
def test_promote_route_refuses_before_the_gate_without_a_usable_audit_store(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: unset, refused or failing-to-open sink -> 503 before the gate; job failed."""

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app
    from recertia.review import promotion_audit

    roots = {
        "unset": "",
        "inside_api_root": str(tmp_path / "api-root" / "audit"),
        "inside_runs_root": str(tmp_path / "api-root" / "runs" / "t1" / "audit"),
        "open_error": str(tmp_path / "operator"),
    }
    monkeypatch.setenv("RECERTIA_OPERATOR_AUDIT_ROOT", roots[case])
    if case == "open_error":

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise sqlite3.OperationalError(f"unable to open {tmp_path}/{FIXTURE}")

        monkeypatch.setattr(promotion_audit, "open_db", _boom)
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    client = TestClient(app)
    ctx = app.state.recertia
    store = SkillStore(ctx.tenant_skills_root("t1"))
    store.write_candidate(_skill(1, command="true", criterion_run="true"))
    gate = _SpyGate()
    monkeypatch.setattr(ctx.promotion_service, "_gate", gate)
    resp = client.post(
        f"/v1/skills/{SKILL}/versions/1/promote", headers=_key(app, {"runs", "promote"})
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["refusal_kind"] == "audit_unavailable"
    assert body["audit_ref"] is None and body["failing_count"] == 0
    assert "the golden gate did not run" in body["error"]
    assert gate.calls == 0
    assert not (ctx.tenant_runs_root("t1") / "promotion_logs").exists()
    assert store.get_status(SKILL, 1).lifecycle == "candidate"
    if case.startswith("inside"):
        assert not Path(roots[case]).exists()
    job = client.get(f"/v1/jobs/{body['job_run_id']}", headers=_key(app, {"runs"}))
    assert job.status_code == 200
    assert job.json()["status"] == "failed"  # never left "running"
    for text in (resp.text, job.text):
        _assert_no_leak(text, tmp_path)
        assert "RECERTIA_OPERATOR_AUDIT_ROOT" not in text  # no deployment config


def test_promote_route_audit_write_failure_mid_way_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the gate ran and refused, then the sink write fails -> 503, job failed."""

    client, app = _app(tmp_path, monkeypatch)
    ctx = app.state.recertia
    SkillStore(ctx.tenant_skills_root("t1")).write_candidate(
        _skill(1, command="true", criterion_run="true")
    )
    fixture_dir = tmp_path / "heldout-src" / "repo-chore" / FIXTURE
    monkeypatch.setattr(ctx.promotion_service, "_gate", _FailingGate(fixture_dir, fail_planted=True))

    def _fail(self: OperatorAuditStore, entry: Any) -> str:
        del self, entry
        raise sqlite3.OperationalError(f"database or disk is full: {fixture_dir}")

    monkeypatch.setattr(OperatorAuditStore, "record", _fail)
    resp = client.post(
        f"/v1/skills/{SKILL}/versions/1/promote", headers=_key(app, {"runs", "promote"})
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["refusal_kind"] == "audit_unavailable"
    assert "the refusal detail was not recorded" in body["error"]
    runs_only = _key(app, {"runs"})
    one = client.get(f"/v1/jobs/{body['job_run_id']}", headers=runs_only)
    many = client.get("/v1/jobs", headers=runs_only)
    assert one.json()["status"] == "failed"
    log = ctx.tenant_runs_root("t1") / "promotion_logs" / f"{SKILL}-v1.json"
    assert not log.exists()
    for text in (resp.text, one.text, many.text):
        _assert_no_leak(text, fixture_dir, tmp_path)


# ----- CLI stderr -----


def test_cli_promote_stderr_omits_fixture_ids_without_operator_detail(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app

    store = SkillStore(tmp_path / "skills")
    store.write_candidate(_skill(1, command="true", criterion_run="test -f MISSING.txt"))
    fixture = _golden(tmp_path / "golden" / "repo-chore" / FIXTURE, command="test -f MISSING.txt")
    args = [
        "skills",
        "promote",
        SKILL,
        "--version",
        "1",
        "--skills-root",
        str(tmp_path / "skills"),
        "--golden-dir",
        str(fixture),
        "--runs-root",
        str(tmp_path / "runs"),
        "--log-dir",
        str(tmp_path / "logs"),
        "--audit-root",
        str(tmp_path / "operator"),
    ]
    runner = CliRunner()
    plain = runner.invoke(app, args)
    assert plain.exit_code == 1, plain.output
    assert "PROMOTION FAILED" in plain.stderr
    _assert_no_leak(plain.output, fixture, tmp_path)
    _assert_no_leak(plain.stderr, fixture, tmp_path)

    detailed = runner.invoke(app, [*args, "--operator-detail"])
    assert detailed.exit_code == 1
    assert FIXTURE in detailed.stderr
    assert FIXTURE not in detailed.stdout


def test_cli_promote_refuses_before_the_gate_without_an_audit_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app

    monkeypatch.delenv("RECERTIA_OPERATOR_AUDIT_ROOT", raising=False)
    store = SkillStore(tmp_path / "skills")
    store.write_candidate(_skill(1, command="true", criterion_run="test -f MISSING.txt"))
    fixture = _golden(tmp_path / "golden" / "repo-chore" / FIXTURE, command="test -f MISSING.txt")
    base = [
        "skills",
        "promote",
        SKILL,
        "--version",
        "1",
        "--skills-root",
        str(tmp_path / "skills"),
        "--golden-dir",
        str(fixture),
        "--runs-root",
        str(tmp_path / "runs"),
        "--log-dir",
        str(tmp_path / "logs"),
    ]
    runner = CliRunner()
    for extra in ([], ["--audit-root", str(tmp_path / "runs" / "audit")]):
        result = runner.invoke(app, [*base, *extra])
        assert result.exit_code == 1, result.output
        assert "PROMOTION REFUSED: operator audit store unavailable" in result.stderr
        assert not (tmp_path / "runs" / "golden-runs").exists()  # the gate never ran
        assert not (tmp_path / "logs").exists()
        _assert_no_leak(result.output, fixture, tmp_path)

    def _fail(self: OperatorAuditStore, entry: Any) -> str:
        del self, entry
        raise sqlite3.OperationalError(f"disk I/O error: {fixture}")

    monkeypatch.setattr(OperatorAuditStore, "record", _fail)
    mid_way = runner.invoke(app, [*base, "--audit-root", str(tmp_path / "operator")])
    assert mid_way.exit_code == 1, mid_way.output
    assert "operator audit store unavailable" in mid_way.stderr
    assert "the refusal detail was not recorded" in mid_way.stderr
    _assert_no_leak(mid_way.output, fixture, tmp_path)
    assert store.get_status(SKILL, 1).lifecycle == "candidate"
