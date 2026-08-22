from __future__ import annotations

from contracts.policy import JobQuota
from recertia.jobs import JobBudget, JobRunner
from recertia.memory.procedural.store import SkillStore


def test_runner_skips_hex_when_quota_exhausted(tmp_path) -> None:
    quota = JobQuota(weekly_token_cap=100, hex_share=0.25)
    quota = quota.charge("recertifier", 100)
    runner = JobRunner(SkillStore(tmp_path / "skills"), quota=quota)
    called = {"n": 0}

    def fn():
        called["n"] += 1
        return []

    result = runner.run("practice_hex", fn, budget=JobBudget(max_tokens=10))
    assert result.skipped
    assert called["n"] == 0
    result = runner.run("fail_cluster_author", fn, budget=JobBudget(max_tokens=0))
    assert result.skipped is None
    assert called["n"] == 1


def test_runner_charges_computer_use_share_when_task_class_set(tmp_path) -> None:
    quota = JobQuota(weekly_token_cap=1000, computer_use_practice_share=0.1)
    runner = JobRunner(SkillStore(tmp_path / "skills"), quota=quota)
    result = runner.run(
        "practice",
        list,
        budget=JobBudget(max_tokens=40),
        task_class="bug-reproduction",
    )
    assert result.skipped is None
    assert runner.quota.computer_use_tokens_spent == 40
    refused = runner.run(
        "practice",
        list,
        budget=JobBudget(max_tokens=70),
        task_class="docs-auditor",
    )
    assert refused.skipped
    assert "quota refused" in refused.skipped
