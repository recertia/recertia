"""Job quota is reserved in SQLite before the job runs and settled after."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contracts.policy import JobQuota
from recertia.faults import FaultInjected
from recertia.jobs import JobBudget, JobRunner
from recertia.jobs.quota_repo import JobQuotaRepository
from recertia.memory.procedural.store import SkillStore
from recertia.policy_load import QuotaSidecar


def test_crash_after_reserve_does_not_double_spend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_ALLOW_FAULTS", "1")
    monkeypatch.setenv("RECERTIA_FAULTS", "quota.after_reserve:kill")
    path = tmp_path / "job_quota.json"
    quota = JobQuota(weekly_token_cap=100)
    runner = JobRunner(SkillStore(tmp_path / "skills"), quota=quota, quota_path=path)
    with pytest.raises(FaultInjected):
        runner.run("recertify", list, budget=JobBudget(max_tokens=60))

    assert runner.quota_repo is not None
    spent, reserved = runner.quota_repo.totals()
    assert spent == 0
    assert reserved == 60
    assert spent + reserved <= 100

    monkeypatch.setenv("RECERTIA_FAULTS", "")
    second = JobRunner(
        SkillStore(tmp_path / "skills2"),
        quota=JobQuota(weekly_token_cap=100),
        quota_path=path,
    )
    second_repo = second.quota_repo
    assert second_repo is not None
    refused = second.run("recertify", list, budget=JobBudget(max_tokens=50))
    assert refused.skipped == "quota refused recertify"
    spent, reserved = second_repo.totals()
    assert spent + reserved == 60

    reservation_id = runner.quota_repo.reserved_ids()[0]
    runner.quota_repo.settle(reservation_id)
    runner.quota_repo.settle(reservation_id)
    assert runner.quota_repo.totals() == (60, 0)

    accepted = second.run("recertify", list, budget=JobBudget(max_tokens=40))
    assert accepted.skipped is None
    assert second_repo.totals() == (100, 0)


def test_computer_use_reservation_persists(tmp_path) -> None:
    path = tmp_path / "job_quota.json"
    quota = JobQuota(weekly_token_cap=1000, computer_use_practice_share=0.1)
    runner = JobRunner(SkillStore(tmp_path / "skills"), quota=quota, quota_path=path)
    result = runner.run(
        "practice",
        list,
        budget=JobBudget(max_tokens=40),
        task_class="bug_reproduction",
    )
    assert result.skipped is None
    assert runner.quota_repo is not None
    assert runner.quota_repo.computer_use_spent() == 40
    assert runner.quota.computer_use_tokens_spent == 40
    saved = QuotaSidecar(path).merge(JobQuota(weekly_token_cap=1000))
    assert saved.computer_use_tokens_spent == 40
    assert saved.tokens_spent == 40


def test_exception_releases_the_reservation(tmp_path) -> None:
    path = tmp_path / "job_quota.json"
    runner = JobRunner(
        SkillStore(tmp_path / "skills"),
        quota=JobQuota(weekly_token_cap=100),
        quota_path=path,
    )

    def boom() -> list:
        raise RuntimeError("job failed")

    with pytest.raises(RuntimeError):
        runner.run("recertify", boom, budget=JobBudget(max_tokens=80))
    assert runner.quota_repo is not None
    assert runner.quota_repo.totals() == (0, 0)
    assert runner.quota_repo.reserved_ids() == []
    follow = runner.run("recertify", list, budget=JobBudget(max_tokens=80))
    assert follow.skipped is None
    assert runner.quota_repo.totals() == (80, 0)


def test_expired_reservation_is_released(tmp_path) -> None:
    repo = JobQuotaRepository(tmp_path / "job_quota.sqlite")
    quota = JobQuota(weekly_token_cap=100)
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    repo.reserve(
        quota,
        job="recertify",
        priority="recertifier",
        tokens=90,
        created_at=stale,
    )
    assert repo.totals() == (0, 90)
    released = repo.release_expired(ttl_s=3600)
    assert released == 1
    assert repo.totals() == (0, 0)
    fresh = repo.reserve(quota, job="recertify", priority="recertifier", tokens=90)
    repo.settle(fresh)
    assert repo.totals() == (90, 0)


def test_sidecar_import_counts_toward_the_cap(tmp_path) -> None:
    path = tmp_path / "job_quota.json"
    QuotaSidecar(path).save(
        JobQuota(weekly_token_cap=100, tokens_spent=70, computer_use_tokens_spent=10)
    )
    runner = JobRunner(
        SkillStore(tmp_path / "skills"),
        quota=JobQuota(weekly_token_cap=100),
        quota_path=path,
    )
    assert runner.quota.tokens_spent == 70
    refused = runner.run("recertify", list, budget=JobBudget(max_tokens=40))
    assert refused.skipped == "quota refused recertify"
    assert runner.quota_repo is not None
    assert runner.quota_repo.totals() == (70, 0)
