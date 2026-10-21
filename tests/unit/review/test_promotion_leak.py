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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from contracts.criteria import SkillCertificationCriterion, TaskCriterion, mint_rejecting_proof
from contracts.skill import FailureMode, Hygiene, Provenance, SkillVersion, Step
from recertia.evals.golden import GoldenReport, GoldenResult
from recertia.memory.procedural.store import SkillStore
from recertia.review.promotion import PromotionError, promote_to_approved
from recertia.review.promotion_audit import OperatorAuditStore

SKILL = "leak-probe"
FIXTURE = "HELDOUT-x"
KINDS = ("golden_gate", "predecessor_non_regression")
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ABS_PATH = re.compile(r"(?:^|[\s'\"=(:\[,])/[^\s'\"/]+/")


def _proven(cid: str, command: str) -> SkillCertificationCriterion:
    base = SkillCertificationCriterion(id=cid, kind="command", run=command, preregistered=True)
    return base.model_copy(
        update={"sensitivity_proof": mint_rejecting_proof(base, fingerprint=f"leak-{cid}")}
    )


def _skill(version: int, *, command: str, criterion_run: str) -> SkillVersion:
    return SkillVersion(
        skill_id=SKILL,
        version=version,
        supersedes=version - 1 if version > 1 else None,
        title=f"Leak probe skill v{version}",
        intent=f"Synthetic skill used only by the REC-009 leak test, version {version}.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run the synthetic fixture command",
                inputs={"command": command},
            )
        ],
        certification_criteria=[_proven("ok", criterion_run)],
        failure_modes=[
            FailureMode(
                symptom="Fixture command exits non-zero on a dirty workspace",
                response="Restore the snapshot and re-run the fixture command",
            )
        ],
        provenance=Provenance(
            distilled_from_run="leak-probe",
            distilled_at=_NOW,
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=_NOW),
    )


def _golden(path: Path, *, command: str, workspace_file: str | None = None) -> Path:
    criterion = TaskCriterion(
        id="gate",
        kind="command",
        run=command,
        source="caller",
        sensitivity_proof=mint_rejecting_proof(
            TaskCriterion(id="gate", kind="command", run=command, source="caller"),
            fingerprint="leak-task-gate",
        ),
    )
    (path / "workspace").mkdir(parents=True)
    if workspace_file:
        (path / "workspace" / workspace_file).write_text("x\n", encoding="utf-8")
    (path / "task.json").write_text(
        json.dumps(
            {
                "request": f"satisfy {command}",
                "expected_skill_id": SKILL,
                "criteria": [criterion.model_dump(mode="json")],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "expect.json").write_text('{"terminal": "solved"}\n', encoding="utf-8")
    return path


def _assert_no_leak(text: str, *paths: Path) -> None:
    assert FIXTURE not in text
    for path in paths:
        assert str(path) not in text
    assert not _ABS_PATH.search(text), f"filesystem path in proposer output: {text!r}"


def _exception_surfaces(exc: BaseException) -> list[str]:
    surfaces = [str(exc), repr(exc), repr(exc.args)]
    surfaces.extend(repr(v) for v in vars(exc).values())
    surfaces.append(repr(getattr(exc, "__notes__", [])))
    assert exc.__cause__ is None and exc.__context__ is None
    if isinstance(exc, PromotionError):
        surfaces.append(json.dumps(exc.decision()))
    return surfaces


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


def test_refusal_without_an_audit_sink_drops_detail(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    log_dir = tmp_path / "logs"
    _refuse("golden_gate", tmp_path, store, log_dir=log_dir, audit=None)
    exc: PromotionError = pytest.refused  # type: ignore[attr-defined]
    assert exc.audit_ref is None
    assert "operator detail not recorded" in str(exc)
    for surface in _exception_surfaces(exc):
        _assert_no_leak(surface, tmp_path)
    _assert_no_leak((log_dir / f"{SKILL}-v1.json").read_text(encoding="utf-8"), tmp_path)


def test_gate_error_detail_goes_to_the_audit_sink_only(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    store.write_candidate(_skill(1, command="true", criterion_run="true"))
    fixture = tmp_path / "golden" / "repo-chore" / FIXTURE
    fixture.mkdir(parents=True)
    (fixture / "task.json").write_text("{not json " + FIXTURE, encoding="utf-8")
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
    record = audit.read(exc.audit_ref or "")
    assert record is not None and record["detail"].startswith("JSONDecodeError")


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


def test_audit_root_inside_the_api_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_OPERATOR_AUDIT_ROOT", str(tmp_path / "api-root" / "audit"))
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    client = TestClient(app)
    ctx = app.state.recertia
    SkillStore(ctx.tenant_skills_root("t1")).write_candidate(
        _skill(1, command="true", criterion_run="true")
    )
    fixture_dir = tmp_path / "heldout-src" / "repo-chore" / FIXTURE
    monkeypatch.setattr(ctx.promotion_service, "_gate", _FailingGate(fixture_dir, fail_planted=True))
    resp = client.post(
        f"/v1/skills/{SKILL}/versions/1/promote", headers=_key(app, {"runs", "promote"})
    )
    body = resp.json()
    assert body["audit_ref"] is None
    assert "operator detail not recorded" in body["error"]
    assert not (tmp_path / "api-root" / "audit").exists()
    _assert_no_leak(resp.text, fixture_dir, tmp_path)


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
