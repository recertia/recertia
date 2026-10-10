"""Sweep JobQuota admit/charge sequences; caps must hold (ADR-0015)."""

from __future__ import annotations

import random

import pytest

from contracts.policy import COMPUTER_USE_TASK_CLASSES, JobQuota

JOBS = (
    "recertifier",
    "curator_retire",
    "fail_cluster_author",
    "practice_band",
    "practice_hex",
    "compress",
)
CU_CLASS = COMPUTER_USE_TASK_CLASSES[0]  # "bug_reproduction"


def test_cu_class_is_real() -> None:
    assert CU_CLASS in COMPUTER_USE_TASK_CLASSES


def test_random_sequences_respect_all_caps() -> None:
    rng = random.Random(0)
    for _ in range(300):
        q = JobQuota(
            weekly_token_cap=rng.choice([0, 1, 1000, 10_000]),
            hex_share=rng.choice([0.0, 0.25, 1.0]),
            computer_use_practice_share=rng.choice([0.0, 0.15]),
        )
        for _ in range(40):
            job = rng.choice(JOBS)
            tc = rng.choice([None, "repo-chore", CU_CLASS])
            tok = rng.randint(1, 3000)
            if q.can_admit(job, task_class=tc, tokens=tok):  # type: ignore[arg-type]
                q = q.charge(job, tok, task_class=tc)  # type: ignore[arg-type]
            assert q.tokens_spent <= q.weekly_token_cap
            assert q.hex_tokens_spent <= int(q.weekly_token_cap * q.hex_share)
            assert q.computer_use_tokens_spent <= int(
                q.weekly_token_cap * q.computer_use_practice_share
            )
            assert all(n <= q.max_hex_jobs_per_task_class for n in q.hex_jobs_by_class.values())
            assert 0 <= q.hex_remaining() <= q.remaining()


def test_unknown_job_fails_closed() -> None:
    assert JobQuota().can_admit("live_trade", tokens=0) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("job", JOBS)
def test_exhausted_cap_rejects_positive_tokens(job: str) -> None:
    q = JobQuota(weekly_token_cap=100, tokens_spent=100)
    assert q.can_admit(job, tokens=1) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("job", JOBS)
def test_exhausted_cap_rejects_zero_token_admission(job: str) -> None:
    """Bug 7 (CTO_REVIEW §4): tokens=0 must not bypass a fully spent weekly cap."""
    q = JobQuota(weekly_token_cap=100, tokens_spent=100)
    assert q.can_admit(job) is False  # type: ignore[arg-type]


def test_exhausted_hex_share_rejects_zero_token_admission() -> None:
    q = JobQuota(weekly_token_cap=1000, hex_share=0.25, hex_tokens_spent=250, tokens_spent=250)
    assert q.can_admit("practice_hex") is False
    assert q.can_admit("recertifier") is True


def test_zero_cap_admits_nothing() -> None:
    for job in JOBS:
        assert JobQuota(weekly_token_cap=0).can_admit(job) is False  # type: ignore[arg-type]
