"""Single owner for named jobs: CLI and HTTP share execute_job."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.jobs.arxiv import ArxivPaper
from recertia.jobs.dispatch import (
    JobDispatchError,
    JobRequest,
    UnknownJob,
    canonical_job_name,
    execute_job,
)
from recertia.memory.procedural.store import SkillStore


def _store(tmp_path: Path) -> tuple[SkillStore, Path]:
    skills = tmp_path / "skills"
    runs = tmp_path / "runs"
    runs.mkdir()
    return SkillStore(skills), runs


def test_canonical_aliases() -> None:
    assert canonical_job_name("miner") == "mine"
    assert canonical_job_name("HEX") == "practice_hex"
    assert canonical_job_name("parallelize") == "parallelise"


def test_unknown_job(tmp_path: Path) -> None:
    store, runs = _store(tmp_path)
    with pytest.raises(UnknownJob, match="unknown job"):
        execute_job(
            JobRequest(name="not-a-job"),
            store=store,
            runs_root=runs,
            skills_root=store.root,
        )


def test_parallelise_requires_skill_id(tmp_path: Path) -> None:
    store, runs = _store(tmp_path)
    with pytest.raises(JobDispatchError, match="skill_id"):
        execute_job(
            JobRequest(name="parallelise"),
            store=store,
            runs_root=runs,
            skills_root=store.root,
        )


def test_mine_defaults_to_hints_not_arxiv(tmp_path: Path) -> None:
    store, runs = _store(tmp_path)
    result = execute_job(
        JobRequest(name="mine", dry_run=True),
        store=store,
        runs_root=runs,
        skills_root=store.root,
    )
    assert result.job == "mine"
    assert result.proposals
    assert result.proposals[0].payload.get("curation") == "mined_from_human_artifact"
    assert "arxiv_id" not in (result.proposals[0].payload or {})


def test_mine_arxiv_uses_stub_client(tmp_path: Path) -> None:
    store, runs = _store(tmp_path)

    class Stub:
        def fetch_by_ids(self, ids):
            return [
                ArxivPaper(
                    arxiv_id="2605.22148",
                    title="Ratchet: hygiene for self-evolving agents xx",
                    abstract="Lifecycle management of skill libraries is largely neglected.",
                    authors=("A",),
                    categories=("cs.AI",),
                )
            ]

        def search(self, query, max_results=5):
            del query, max_results
            return []

    result = execute_job(
        JobRequest(name="miner", dry_run=True, arxiv_id=["2605.22148"], arxiv_client=Stub()),
        store=store,
        runs_root=runs,
        skills_root=store.root,
    )
    assert result.job == "mine"
    assert result.proposals[0].payload.get("curation") == "mined_from_paper"
    assert result.proposals[0].payload.get("arxiv_id", "").startswith("2605.22148")


def test_with_pdf_without_arxiv_is_noop(tmp_path: Path) -> None:
    store, runs = _store(tmp_path)
    result = execute_job(
        JobRequest(name="mine", dry_run=True, with_pdf=True),
        store=store,
        runs_root=runs,
        skills_root=store.root,
    )
    assert "pdf_path" not in (result.proposals[0].payload or {})


def test_practice_one_off(tmp_path: Path) -> None:
    store, runs = _store(tmp_path)
    result = execute_job(
        JobRequest(name="practice", dry_run=True, one_off=["cluster-a"]),
        store=store,
        runs_root=runs,
        skills_root=store.root,
    )
    assert result.job == "practice"
    assert result.proposals


def test_cli_jobs_run_mine_dry_run(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from recertia.cli.main import app

    runner = CliRunner()
    ok = runner.invoke(
        app,
        [
            "jobs",
            "run",
            "mine",
            "--dry-run",
            "--runs-root",
            str(tmp_path / "runs"),
            "--skills-root",
            str(tmp_path / "skills"),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert "mined_from_human_artifact" in ok.output
    assert "arxiv_id" not in ok.output
