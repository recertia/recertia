"""A job runner closes the quota database it opens, and the API and CLI job runs close their runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contracts.policy import JobQuota
from recertia.jobs import JobRunner
from recertia.jobs.quota_repo import JobQuotaRepository
from recertia.memory.procedural.store import SkillStore


class _Boom(Exception):
    pass


def _count_repo_closes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    closes: list[int] = []
    real_close = JobQuotaRepository.close

    def spy(self: JobQuotaRepository) -> None:
        closes.append(1)
        real_close(self)

    monkeypatch.setattr(JobQuotaRepository, "close", spy)
    return closes


def _count_runner_closes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    closes: list[int] = []
    real_close = getattr(JobRunner, "close", None)

    def spy(self: JobRunner) -> None:
        closes.append(1)
        if real_close is not None:
            real_close(self)

    monkeypatch.setattr(JobRunner, "close", spy, raising=False)
    return closes


def _runner(tmp_path: Path) -> JobRunner:
    return JobRunner(
        SkillStore(tmp_path / "skills"),
        quota=JobQuota(weekly_token_cap=100),
        quota_path=tmp_path / "job_quota.json",
    )


def test_close_closes_the_quota_repo_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closes = _count_repo_closes(monkeypatch)
    runner = _runner(tmp_path)
    repo = runner.quota_repo
    assert repo is not None

    runner.close()

    assert closes == [1]
    with pytest.raises(Exception, match="closed"):
        repo.totals()


def test_a_second_close_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closes = _count_repo_closes(monkeypatch)
    runner = _runner(tmp_path)

    runner.close()
    runner.close()

    assert closes == [1]


def test_close_without_a_quota_path_does_nothing(tmp_path: Path) -> None:
    runner = JobRunner(SkillStore(tmp_path / "skills"))
    assert runner.quota_repo is None

    runner.close()
    runner.close()


@pytest.mark.parametrize("step", ["import_sidecar", "seed_open_week", "snapshot"])
def test_setup_failure_closes_the_quota_repo_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    closes = _count_repo_closes(monkeypatch)
    error = _Boom(step)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(JobQuotaRepository, step, fail)

    with pytest.raises(_Boom) as raised:
        _runner(tmp_path)

    assert raised.value is error
    assert closes == [1]


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, str]]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
    app = create_app(root=tmp_path / "api-root", skills_root=tmp_path / "skills")
    issued = app.state.api_keys.issue(
        tenant_id="t1", scopes={"runs", "metrics", "exec", "admin"}, actor="test"
    )
    return TestClient(app), {"X-API-Key": issued.secret}


def test_api_job_run_closes_its_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _api(tmp_path, monkeypatch)
    closes = _count_runner_closes(monkeypatch)

    resp = client.post(
        "/v1/jobs/practice/run", headers=headers, json={"dry_run": True, "one_off": ["cluster-a"]}
    )

    assert resp.status_code == 200, resp.text
    assert closes == [1]


def test_api_job_run_closes_its_runner_when_the_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.app.jobs import JobService

    client, headers = _api(tmp_path, monkeypatch)
    closes = _count_runner_closes(monkeypatch)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise _Boom("job failed")

    monkeypatch.setattr(JobService, "run", fail)

    resp = client.post("/v1/jobs/practice/run", headers=headers, json={"dry_run": True})

    assert resp.status_code == 400
    assert closes == [1]


def _cli_args(tmp_path: Path) -> list[str]:
    return [
        "jobs",
        "run",
        "practice",
        "--dry-run",
        "--one-off",
        "cluster-a",
        "--skills-root",
        str(tmp_path / "skills"),
        "--runs-root",
        str(tmp_path / "runs"),
    ]


def test_cli_job_run_closes_its_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app

    closes = _count_runner_closes(monkeypatch)

    result = CliRunner().invoke(app, _cli_args(tmp_path))

    assert result.exit_code == 0, result.output
    assert closes == [1]


def test_cli_job_run_closes_its_runner_when_the_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from recertia.app.jobs import JobService
    from recertia.cli.main import app

    closes = _count_runner_closes(monkeypatch)
    error = _Boom("job failed")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(JobService, "run", fail)

    result = CliRunner().invoke(app, _cli_args(tmp_path))

    assert result.exception is error
    assert closes == [1]
