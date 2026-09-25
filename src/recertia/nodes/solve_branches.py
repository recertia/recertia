"""Fan-out branch execution. Owns the public validate scoring dependency."""

from __future__ import annotations

import time
from dataclasses import replace

from contracts.budget import BudgetReservation, Spend, budget_excess
from contracts.criteria import CriterionResult
from contracts.failure import FailureSignal
from contracts.run import RunState
from recertia.nodes._util import now
from recertia.nodes.attempt import AttemptMeter, completed, failed
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.nodes.solve_script import run_container_command
from recertia.nodes.validate import score_owned_criteria


def solve_branches(state: RunState, ctx: NodeContext, attempt_no: int) -> NodeOutcome:
    # Branch leases are being retired into parent spend here, so the parent must not also be
    # charged for still holding the reservation fan_out took out.
    meter = AttemptMeter.open(state, reserved=BudgetReservation())
    updated = []
    attempts_charged = 0
    for branch in state.branches:
        if branch.status not in ("dispatched", "running"):
            updated.append(branch)
            continue
        work = ctx.workdir / branch.branch_id
        work.mkdir(parents=True, exist_ok=True)
        script = ctx.script or ["true"]
        # Enforce the branch lease before spending tools — the lease is not advisory.
        projected = Spend(
            attempts=branch.spent.attempts + 1,
            tool_calls=branch.spent.tool_calls + len(script),
            tokens=branch.spent.tokens,
            wall_clock_s=branch.spent.wall_clock_s,
            cost_usd=branch.spent.cost_usd + 0.01 * (1 + len(script)),
        )
        branch_excess = budget_excess(
            branch.budget, projected, BudgetReservation(), BudgetReservation()
        )
        if branch_excess is not None:
            updated.append(
                branch.model_copy(
                    update={
                        "status": "timed_out",
                        "results": [
                            CriterionResult(
                                criterion_id="branch-budget",
                                kind="command",
                                passed=False,
                                weight=1.0,
                            )
                        ],
                        "workspace_ref": str(work),
                        "spent": projected,
                        "reserved": BudgetReservation(),
                    }
                )
            )
            # Wall clock comes from the meter's own clock, which spans the whole branch loop,
            # rather than from summing per-branch measurements.
            meter.charge(
                tool_calls=projected.tool_calls,
                tokens=projected.tokens,
                cost_usd=projected.cost_usd,
            )
            attempts_charged += projected.attempts
            continue
        ok = True
        started = time.monotonic()
        executed = 0
        for command in script:
            result = run_container_command(command, work)
            executed += 1
            if result["returncode"] != 0:
                ok = False
                break
        # Decomposition branches own and score only their assigned criteria. Portfolio
        # branches retain their lightweight comparable score; the selected artifact is
        # subsequently validated as a whole by join.
        if branch.kind == "decomposition":
            owned = [c for c in state.criteria if c.id in branch.owned_criteria]
            branch_ctx = replace(ctx, workdir=work, node="validate")
            results = score_owned_criteria(owned, branch_ctx)
            ok = ok and all(result.passed for result in results if result.weight >= 1.0)
        else:
            results = [
                CriterionResult(criterion_id="branch-ok", kind="command", passed=ok, weight=1.0)
            ]
        cost = 0.01 * (1 + executed)
        elapsed = time.monotonic() - started
        branch_spent = Spend(
            attempts=1,
            tool_calls=executed,
            wall_clock_s=elapsed,
            cost_usd=cost,
        )
        meter.charge(
            tool_calls=branch_spent.tool_calls,
            tokens=branch_spent.tokens,
            cost_usd=branch_spent.cost_usd,
        )
        attempts_charged += branch_spent.attempts
        updated.append(
            branch.model_copy(
                update={
                    "status": "succeeded" if ok else "failed",
                    "results": results,
                    "cost_usd": cost,
                    "workspace_ref": str(work),
                    "spent": branch_spent,
                    "reserved": BudgetReservation(),
                }
            )
        )

    # Reconcile each lease into parent spend. This catches a runtime measurement that was
    # larger than its conservative admission estimate without hiding the actual spend.
    retired = {"reserved": BudgetReservation(), "branches": updated}
    exhausted = meter.preflight(attempts=attempts_charged)
    if exhausted is not None:
        return failed(
            state,
            meter,
            signal=FailureSignal(
                source="solver",
                detail=f"parent budget exceeded by branches: {exhausted}",
                at=now(),
                class_hint="budget",
            ),
            attempt_no=attempt_no,
            attempts=attempts_charged,
            note=f"budget exceeded: {exhausted}",
            updates={**retired, "transcript_ref": f"{ctx.run_id}/branches-{attempt_no}"},
        )

    return completed(
        state,
        meter,
        attempt_no=attempt_no,
        attempts=attempts_charged,
        transcript_ref=f"{ctx.run_id}/branches-{attempt_no}",
        note=f"ran {len(updated)} branches",
        updates=retired,
    )
