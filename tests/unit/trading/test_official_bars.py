"""Official tape, proposal-only trading job, no approved write."""

from __future__ import annotations

from pathlib import Path

from recertia.app.jobs import JobDispatch, JobService
from recertia.jobs import resolve_job_priority
from recertia.ports.identity import Principal
from recertia.trading.bars import default_bar_path, load_official_bars
from recertia.trading.proposals import run_trading_tape, trading_proposal

ROOT = Path(__file__).resolve().parents[3]


def test_default_tape_is_the_plotly_aapl_file() -> None:
    path = default_bar_path()
    assert path == ROOT / "data" / "official" / "aapl-plotly.csv"
    bars = load_official_bars(path)
    assert bars[0].date == "2015-02-17"
    assert bars[0].symbol == "AAPL"
    assert bars[0].close == 127.830002
    assert len(bars) == 506
    result = run_trading_tape(path)
    assert result.proposals == []
    assert result.skipped == "horizon_remainder"


def test_unstamped_file_is_refused() -> None:
    missing = ROOT / "data" / "official" / "missing.csv"
    refused = run_trading_tape(missing)
    assert refused.proposals == []
    assert refused.skipped is not None
    assert "missing official bar file" in refused.skipped


def test_trading_job_is_a_curator_class_proposal() -> None:
    spec = JobService(pdp=None).resolve("trading_propose")
    assert spec is not None
    assert spec.name == "trading"
    assert resolve_job_priority("trading") == "curator_retire"
    handler = (ROOT / "src/recertia/app/jobs.py").read_text(encoding="utf-8")
    body = handler[handler.index("def _run_trading") :]
    assert "write_candidate" not in body
    assert "submit_proposal" not in body
    dispatch = JobDispatch(
        store=object(),
        runner=object(),
        budget=object(),
        policy=object(),
        runs_root=ROOT,
        lineage=object(),
    )
    result = JobService(pdp=None).run(
        Principal.local_operator(),
        "trading",
        dispatch=dispatch,
    )
    assert result.proposals == []
    assert result.skipped == "horizon_remainder"


def test_null_contribution_cannot_become_a_proposal() -> None:
    try:
        trading_proposal("retirement", "macro", "drop", contribution=None, evidence_met=True)
    except ValueError as exc:
        assert "null contribution" in str(exc)
    else:
        raise AssertionError("null contribution was proposed")
    proposal = trading_proposal("rewrite", "macro", "tighter stop")
    assert proposal.kind == "curate"
    assert proposal.payload["status"] == "proposed"
    assert proposal.payload["writes_approved"] is False
