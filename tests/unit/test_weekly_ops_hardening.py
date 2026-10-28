"""REC-022: weekly-ops.yml is least-privilege and publishes aggregate counts, not eval output."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/weekly-ops.yml"


def _summary_module():
    path = REPO / "scripts/weekly_ops_summary.py"
    spec = importlib.util.spec_from_file_location("weekly_ops_summary", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WORKFLOWS = sorted(
    [*(REPO / ".github/workflows").glob("*.yml"), *(REPO / ".github/workflows").glob("*.yaml")]
)


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), path
    return doc


def _run_blocks(doc: dict) -> list[str]:
    """Every ``run:`` string in every job step, parsed with yaml.safe_load."""

    blocks: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and "run" in step:
                blocks.append(str(step["run"]))
    return blocks


def _inline_expressions(doc: dict) -> list[str]:
    return [block for block in _run_blocks(doc) if "${{" in block]


def test_permissions_are_least_privilege() -> None:
    doc = _load(WORKFLOW)
    assert doc["permissions"] == {"contents": "read"}
    jobs = doc["jobs"]
    assert sorted(jobs) == ["gitleaks-full-history", "metrics-report", "postgres-soak"]
    for name, job in jobs.items():
        assert job.get("permissions") == {"contents": "read"}, name


def test_weekly_ops_actions_are_sha_pinned() -> None:
    uses = [
        step["uses"]
        for job in _load(WORKFLOW)["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and "uses" in step
    ]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_no_eval_output_artifact() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "upload-artifact" not in text
    assert "scripts/weekly_ops_summary.py" in text and "$GITHUB_STEP_SUMMARY" in text
    assert len(_run_blocks(_load(WORKFLOW))) >= 8


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_inline_expressions_in_any_workflow_run(path: Path) -> None:
    """#103: every workflow, not only weekly-ops. Contexts go through ``env:``."""

    assert _inline_expressions(_load(path)) == []


def test_workflow_glob_covers_every_workflow() -> None:
    names = {p.name for p in WORKFLOWS}
    assert {"ci.yml", "nightly.yml", "weekly-ops.yml"} <= names


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_scanner_flags_an_injected_expression(path: Path) -> None:
    """Self-test: injecting ``${{ github.event.* }}`` into a run: (block or flow style) is caught."""

    doc = _load(path)
    target = next(
        step
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and "run" in step
    )
    target["run"] = str(target["run"]) + '\necho "${{ github.event.head_commit.message }}"\n'
    for dumped in (yaml.safe_dump(doc), yaml.safe_dump(doc, default_flow_style=True)):
        assert len(_inline_expressions(yaml.safe_load(dumped))) == 1


def test_summary_prints_only_allowlisted_aggregates(tmp_path: Path) -> None:
    mod = _summary_module()
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "probes.json").write_text(
        json.dumps(
            {
                "precision_at_3": 0.9,
                "skill_count": 11,
                "probes": [{"probe_id": "zz-probe-id", "top3": ["zz-skill-a"], "relevant": ["zz-skill-b"]}],
            }
        )
    )
    (art / "weekly-metrics.json").write_text(
        json.dumps(
            {"report": {"first_attempt_success": 0.5, "causal_lift": {"status": "Free text /path zz-x"}}}
        )
    )
    (art / "soak-log.json").write_text(
        json.dumps({"weeks": [{"counted": False, "reason": "empty_eval_db", "observation_count": 0}]})
    )
    out = mod.summarise(art)
    assert "zz-" not in out and "/path" not in out
    assert "| probe_count | 1 |" in out and "| precision_at_3 | 0.9 |" in out
    assert "| causal_lift.status | (omitted) |" in out
    assert "| soak.latest_reason | empty_eval_db |" in out
    assert "| canary.trials | (missing) |" in out


def test_summary_string_fields_are_closed_enums(tmp_path: Path) -> None:
    """An id-shaped token in a string field is omitted, not printed."""

    mod = _summary_module()
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "weekly-metrics.json").write_text(
        json.dumps({"report": {"causal_lift": {"status": "zz-skill-id"}}})
    )
    (art / "soak-log.json").write_text(json.dumps({"weeks": [{"reason": "zz-fixture-id"}]}))
    out = mod.summarise(art)
    assert "zz-" not in out
    assert "| causal_lift.status | (omitted) |" in out and "| soak.latest_reason | (omitted) |" in out
    (art / "weekly-metrics.json").write_text(
        json.dumps({"report": {"causal_lift": {"status": "low_run_count"}}})
    )
    assert "| causal_lift.status | low_run_count |" in mod.summarise(art)
    assert set(mod.STRING_ENUMS) == {"causal_lift.status", "soak.latest_reason"}
