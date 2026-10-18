"""CLI and API resolve job names through one table."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from recertia.app.jobs import JobDispatch, JobService, canonical_job, known_job_names
from recertia.errors import Forbidden, NotFound
from recertia.ports.identity import Principal

ROOT = Path(__file__).resolve().parents[3]


def test_cli_and_api_share_one_registry() -> None:
    cli = (ROOT / "src/recertia/cli/jobs.py").read_text(encoding="utf-8")
    api = (ROOT / "src/recertia/api/routes/jobs.py").read_text(encoding="utf-8")
    assert "JobService" in cli
    assert "job_service.run" in api
    assert 'name in {"mine"' not in cli
    assert 'name in {"mine"' not in api
    assert canonical_job("miner") == "mine"
    assert canonical_job("curate") == "curator"
    assert canonical_job("recertifier") == "recertify"
    assert canonical_job("parallelize") == "parallelise"
    assert canonical_job("practice_hex") == "hex"
    assert canonical_job("fail_cluster_author") == "fail_cluster"
    assert canonical_job("not-a-job") is None
    assert "mine" in known_job_names()
    assert "shadow" in known_job_names()
    assert "fail_cluster" in known_job_names()


def test_unknown_job_is_not_found() -> None:
    service = JobService(pdp=None)
    principal = Principal.local_operator()
    with pytest.raises(NotFound, match="unknown job"):
        service.run(principal, "not-a-job", dispatch=_empty_dispatch())


def test_jobs_run_requires_jobs_scope() -> None:
    class _Pdp:
        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del resource
            if action != "jobs.run" or not principal.has_scope("jobs"):
                raise Forbidden("missing scope: jobs")

    service = JobService(pdp=_Pdp())
    runs = Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )
    with pytest.raises(Forbidden, match="jobs"):
        service.run(runs, "practice", dispatch=_empty_dispatch())


def _empty_dispatch() -> JobDispatch:
    return JobDispatch(
        store=SimpleNamespace(),
        runner=SimpleNamespace(),
        budget=SimpleNamespace(),
        policy=SimpleNamespace(),
        runs_root=Path("."),
        lineage=SimpleNamespace(),
    )
