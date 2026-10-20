"""REC-022: weekly-ops.yml is least-privilege and publishes aggregate counts, not eval output."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/weekly-ops.yml"


def _summary_module():
    path = REPO / "scripts/weekly_ops_summary.py"
    spec = importlib.util.spec_from_file_location("weekly_ops_summary", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        indent, inline = len(m.group(1)), m.group(2)
        body = [] if inline in ("|", ">", "|-", ">-") else [inline]
        i += 1
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            body.append(lines[i])
            i += 1
        blocks.append("\n".join(body))
    return blocks


def test_permissions_are_least_privilege() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "\npermissions:\n  contents: read\n\njobs:" in text
    jobs = re.findall(r"^  ([a-z0-9-]+):\n    runs-on:", text, flags=re.MULTILINE)
    assert jobs == ["gitleaks-full-history", "metrics-report", "postgres-soak"]
    grants = re.findall(r"^    permissions:\n((?:      .+\n)+)", text, flags=re.MULTILINE)
    assert len(grants) == len(jobs)
    assert all(g.strip() == "contents: read" for g in grants), grants
    assert "write" not in "".join(grants)


def test_no_eval_output_artifact_and_no_inline_expressions() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "upload-artifact" not in text
    assert "scripts/weekly_ops_summary.py" in text and "$GITHUB_STEP_SUMMARY" in text
    blocks = _run_blocks(text)
    assert len(blocks) >= 8
    for block in blocks:
        assert "${{" not in block, block


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
