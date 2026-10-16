"""CLI and API resolve job names through one table."""

from __future__ import annotations

from pathlib import Path

from recertia.app.jobs import canonical_job, known_job_names

ROOT = Path(__file__).resolve().parents[3]


def test_cli_and_api_share_one_registry() -> None:
    cli = (ROOT / "src/recertia/cli/jobs.py").read_text(encoding="utf-8")
    api = (ROOT / "src/recertia/api/console_library_routes.py").read_text(encoding="utf-8")
    assert "canonical_job" in cli
    assert "canonical_job" in api
    assert canonical_job("miner") == "mine"
    assert canonical_job("curate") == "curator"
    assert canonical_job("recertifier") == "recertify"
    assert canonical_job("parallelize") == "parallelise"
    assert canonical_job("practice_hex") == "hex"
    assert canonical_job("not-a-job") is None
    assert "mine" in known_job_names()
    assert "shadow" in known_job_names()
