#!/usr/bin/env python3
"""Generate schema/*.schema.json from the contracts/ Pydantic models (ADR-0009).

Usage:
    python3 scripts/generate_schemas.py            # write into schema/
    python3 scripts/generate_schemas.py --check     # write into a temp dir and diff; exit 1 on drift

Never hand-edit the files this writes. If a schema needs to change, change the model in
``contracts/`` and re-run this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from contracts.applicability import ApplicabilityReport, EnvironmentModel  # noqa: E402
from contracts.branch import BranchState, MergeAudit  # noqa: E402
from contracts.cluster import FailureClusterRow  # noqa: E402
from contracts.eval import CausalLiftResult, ControlBaseline, EvalObservation, MetricReport  # noqa: E402
from contracts.fact import Fact  # noqa: E402
from contracts.failure import FailureSignal, FailureVerdict  # noqa: E402
from contracts.faithfulness import FaithfulnessReport  # noqa: E402
from contracts.goal import Goal  # noqa: E402
from contracts.guide import ExecutionGuide  # noqa: E402
from contracts.ledger import LedgerEntry  # noqa: E402
from contracts.lint import LintReport  # noqa: E402
from contracts.node import CheckpointRecord, NodeOutput  # noqa: E402
from contracts.patch import PatchTemplate  # noqa: E402
from contracts.policy import AuthoringPrior, JobQuota, Policy  # noqa: E402
from contracts.program import MigrationProgram  # noqa: E402
from contracts.review import ReviewDecision  # noqa: E402
from contracts.run import RunState  # noqa: E402
from contracts.scope import ScopePromotion  # noqa: E402
from contracts.skill import SkillVersion  # noqa: E402
from contracts.stats import RetrievalAblationEffect, SkillStats  # noqa: E402
from contracts.status import SkillStatus  # noqa: E402
from contracts.trajectory_import import TrajectoryImport  # noqa: E402
from contracts.workspace import RegisteredWorkspace  # noqa: E402

# Canonical JSON Schema $id prefix (must match the public GitHub org/repo).
SCHEMA_ID_BASE = "https://github.com/recertia/recertia/schema"

MODELS: dict[str, type] = {
    "skill_version.schema.json": SkillVersion,
    "skill_status.schema.json": SkillStatus,
    "skill_stats.schema.json": SkillStats,
    "retrieval_ablation_effect.schema.json": RetrievalAblationEffect,
    "run.schema.json": RunState,
    "branch.schema.json": BranchState,
    "merge_audit.schema.json": MergeAudit,
    "failure_signal.schema.json": FailureSignal,
    "failure_verdict.schema.json": FailureVerdict,
    "ledger_entry.schema.json": LedgerEntry,
    "fact.schema.json": Fact,
    "review_decision.schema.json": ReviewDecision,
    "authoring_prior.schema.json": AuthoringPrior,
    "policy.schema.json": Policy,
    "job_quota.schema.json": JobQuota,
    "failure_cluster_row.schema.json": FailureClusterRow,
    "lint_report.schema.json": LintReport,
    "execution_guide.schema.json": ExecutionGuide,
    "patch_template.schema.json": PatchTemplate,
    "node_output.schema.json": NodeOutput,
    "checkpoint_record.schema.json": CheckpointRecord,
    "scope_promotion.schema.json": ScopePromotion,
    "causal_lift_result.schema.json": CausalLiftResult,
    "control_baseline.schema.json": ControlBaseline,
    "eval_observation.schema.json": EvalObservation,
    "metric_report.schema.json": MetricReport,
    "applicability_report.schema.json": ApplicabilityReport,
    "environment_model.schema.json": EnvironmentModel,
    "faithfulness_report.schema.json": FaithfulnessReport,
    "goal.schema.json": Goal,
    "migration_program.schema.json": MigrationProgram,
    "registered_workspace.schema.json": RegisteredWorkspace,
    "trajectory_import.schema.json": TrajectoryImport,
}


def render(model: type) -> str:
    schema = model.model_json_schema()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SCHEMA_ID_BASE}/{model.__name__}",
        **schema,
    }
    return json.dumps(schema, indent=2, sort_keys=False) + "\n"


def write_all(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in MODELS.items():
        (target_dir / filename).write_text(render(model))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if schema/ would change")
    args = parser.parse_args()

    schema_dir = REPO_ROOT / "schema"

    if not args.check:
        write_all(schema_dir)
        print(f"Wrote {len(MODELS)} schema file(s) to {schema_dir}")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        write_all(tmp_path)
        drift = []
        for filename in MODELS:
            generated = (tmp_path / filename).read_text()
            existing = (schema_dir / filename).read_text() if (schema_dir / filename).exists() else None
            if generated != existing:
                drift.append(filename)
        if drift:
            print("Schema drift detected in:", ", ".join(drift))
            print("Run `python3 scripts/generate_schemas.py` and commit the result.")
            return 1
        print("schema/ matches contracts/ — no drift.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
