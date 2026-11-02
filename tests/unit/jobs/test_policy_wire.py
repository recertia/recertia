from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from contracts.policy import JobQuota
from recertia.jobs import JobBudget, JobRunner, build_job_runner
from recertia.memory.procedural.store import SkillStore
from recertia.policy_load import QuotaSidecar, iso_week_id, load_policy


def test_policy_loads_and_merges_quota_sidecar(tmp_path: Path) -> None:
    policy = load_policy()
    assert policy.version == "p-2026.08.2"
    assert policy.min_independent_runs == 5
    assert policy.faithfulness_interventions_enabled is False
    assert policy.improvement.practice_hex_search is False
    assert policy.job_quota.weekly_token_cap == 500_000

    sidecar = QuotaSidecar(tmp_path / "job_quota.json")
    spent = policy.job_quota.model_copy(
        update={"tokens_spent": 12_000, "hex_tokens_spent": 100, "hex_jobs_by_class": {"x": 1}}
    )
    sidecar.save(spent, at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    merged = sidecar.merge(policy.job_quota, at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    assert merged.tokens_spent == 12_000
    assert merged.hex_jobs_by_class["x"] == 1

    rolled = sidecar.merge(policy.job_quota, at=datetime(2026, 8, 24, tzinfo=timezone.utc))
    assert rolled.tokens_spent == 0
    assert rolled.hex_jobs_by_class == {}
    assert iso_week_id(datetime(2026, 8, 12, tzinfo=timezone.utc)) != iso_week_id(
        datetime(2026, 8, 24, tzinfo=timezone.utc)
    )


def test_build_job_runner_uses_policy_cap(tmp_path: Path) -> None:
    policy = load_policy()
    with closing(
        build_job_runner(
            SkillStore(tmp_path / "skills"),
            runs_root=tmp_path / "jobs",
            policy=policy,
        )
    ) as runner:
        assert runner.quota.weekly_token_cap == policy.job_quota.weekly_token_cap
        assert runner.quota_path == tmp_path / "jobs" / "job_quota.json"


def test_runner_persists_charge(tmp_path: Path) -> None:
    path = tmp_path / "job_quota.json"
    quota = JobQuota(weekly_token_cap=1_000)
    with closing(JobRunner(SkillStore(tmp_path / "skills"), quota=quota, quota_path=path)) as runner:
        result = runner.run("recertify", list, budget=JobBudget(max_tokens=40))
    assert result.skipped is None
    saved = QuotaSidecar(path).merge(JobQuota(weekly_token_cap=1_000))
    assert saved.tokens_spent == 40


def test_cli_and_console_construct_runner_with_quota(tmp_path: Path) -> None:
    # Guard against the old `JobRunner(store)` default-empty-quota path.
    from recertia.jobs import JobRunner as Bare

    bare = Bare(SkillStore(tmp_path / "skills"))
    with closing(build_job_runner(SkillStore(tmp_path / "skills"), runs_root=tmp_path / "jobs")) as wired:
        assert wired.quota.weekly_token_cap != 0
        assert wired.quota.weekly_token_cap == load_policy().job_quota.weekly_token_cap
        # Bare default is the model default — same cap — but has no sidecar.
        assert bare.quota_path is None
        assert wired.quota_path is not None
