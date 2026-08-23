"""``recertia`` CLI wiring: ``run``, ``runs``, ``ledger``, ``skills``, ``keys``, ``lift``, ``jobs``, ``gc``, ``task-state``.

Command implementations live in sibling modules; this file builds the Typer app and
re-exports the historical command callables for tests that import them from ``main``.
"""

from __future__ import annotations

import typer

from recertia.cli.backup_cmd import register_backup_commands
from recertia.cli.canary_cmd import register_canary_commands
from recertia.cli.cases_cmd import register_cases_commands
from recertia.cli.eval_cmd import register_eval_commands
from recertia.cli.facts_cmd import register_facts_commands
from recertia.cli.faithfulness_cmd import register_faithfulness_commands
from recertia.cli.gc import gc_cmd, register_gc_commands
from recertia.cli.jobs import jobs_run, register_jobs_commands
from recertia.cli.keys import keys_issue, keys_list, keys_revoke, register_keys_commands
from recertia.cli.lift import lift_cmd, register_lift_commands
from recertia.cli.memory_cmd import register_memory_commands
from recertia.cli.metrics_cmd import metrics_cmd, register_metrics_commands
from recertia.cli.policy_cmd import register_policy_commands
from recertia.cli.probes import register_probes_commands
from recertia.cli.proposals_cmd import register_proposals_commands
from recertia.cli.review_cmd import register_review_commands
from recertia.cli.runs import (
    ledger_verify,
    register_run_commands,
    resume_cmd,
    run_cmd,
    runs_show,
)
from recertia.cli.skills import (
    register_skills_commands,
    skills_lint,
    skills_promote,
    skills_search,
)
from recertia.cli.soak_cmd import register_soak_commands
from recertia.cli.tabletop_cmd import register_tabletop_commands
from recertia.cli.task_state_cmd import register_task_state_commands
from recertia.cli.workspaces import register_workspaces_commands

app = typer.Typer(help="Recertia: a self-improving agent system.")
register_run_commands(app)
register_skills_commands(app)
register_keys_commands(app)
register_lift_commands(app)
register_metrics_commands(app)
register_probes_commands(app)
register_eval_commands(app)
register_faithfulness_commands(app)
register_policy_commands(app)
register_memory_commands(app)
register_review_commands(app)
register_facts_commands(app)
register_cases_commands(app)
register_proposals_commands(app)
register_jobs_commands(app)
register_gc_commands(app)
register_workspaces_commands(app)
register_backup_commands(app)
register_tabletop_commands(app)
register_canary_commands(app)
register_soak_commands(app)
register_task_state_commands(app)

__all__ = [
    "app",
    "gc_cmd",
    "jobs_run",
    "keys_issue",
    "keys_list",
    "keys_revoke",
    "ledger_verify",
    "lift_cmd",
    "metrics_cmd",
    "resume_cmd",
    "run_cmd",
    "runs_show",
    "skills_lint",
    "skills_promote",
    "skills_search",
]


if __name__ == "__main__":
    app()
