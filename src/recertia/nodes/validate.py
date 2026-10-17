"""``validate``: score locked criteria against the attempt (specs §4, §15.2, §26.3).

Supports ``command``, ``assertion``, ``schema``, ``metric``, and ``judge``. Judges run in
an isolated artifact+rubric context with ``context_hash`` recorded so isolation is testable.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence

from contracts.criteria import CriterionResult, SkillCertificationCriterion, TaskCriterion
from contracts.failure import FailureSignal
from contracts.run import RunState
from recertia.nodes._util import now
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.ports.execution import CommandRequest, CommandResult
from recertia.validation.criteria import CriterionRunner, JudgeContext
from recertia.validation.criteria import PathEscapeError as PathEscapeError  # noqa: F401
from recertia.validation.criteria import _resolve_path as _resolve_path  # noqa: F401
from recertia.validation.judge import assert_distinct_lenses

CriterionLike = TaskCriterion | SkillCertificationCriterion


_CERT_OBS_OP_BASE = 10_000


def validate(state: RunState, ctx: NodeContext) -> NodeOutcome:
    results, failure_signal, downgrade_notes = score_criteria(state, ctx)
    observations = score_certification_observations(state, ctx)
    new_state = state.model_copy(
        update={
            "results": results,
            "results_history": [*state.results_history, results],
            "failure_signal": failure_signal,
            "certification_observations": observations,
        }
    )

    route = "no_branches_and_failing" if failure_signal is not None else "no_branches_and_passing"
    if state.branches:
        route = "has_branches"
    notes = list(downgrade_notes)
    failed_obs = [o.criterion_id for o in observations if not o.passed]
    if failed_obs:
        notes.append(
            "certification_observations failed (advisory, does not gate the caller): "
            + ",".join(failed_obs)
        )
    note = "; ".join(notes) if notes else None
    return NodeOutcome(state=new_state, route=route, note=note)


def score_criteria(
    state: RunState, ctx: NodeContext, *, op_offset: int = 0
) -> tuple[list[CriterionResult], FailureSignal | None, list[str]]:
    """Score a state against ``ctx.workdir`` for normal and post-merge validation."""

    results: list[CriterionResult] = []
    downgrade_notes: list[str] = []
    any_effectively_required_failed = False

    assert_distinct_lenses(list(state.criteria))

    for op_seq, criterion in enumerate(state.criteria, start=op_offset):
        raw = ctx.op_once(op_seq, functools.partial(_score_criterion_dict, criterion, ctx))
        outcome_result = CriterionResult.model_validate(raw)
        actual_passed = outcome_result.passed
        effectively_required = criterion.is_required and criterion.is_preregistered_and_proven

        if criterion.is_required and not effectively_required:
            recorded_passed = True  # advisory downgrade: never gates routing (specs §15.2)
            downgrade_notes.append(
                f"{criterion.id}: required, no valid sensitivity_proof -> advisory "
                f"(actual={'pass' if actual_passed else 'fail'})"
            )
            # Preserve isolation evidence even when advisory.
            results.append(
                outcome_result.model_copy(
                    update={
                        "passed": recorded_passed,
                        "output_excerpt": (
                            f"[advisory] actual={'pass' if actual_passed else 'fail'}; "
                            + outcome_result.output_excerpt
                        )[:2000],
                    }
                )
            )
        else:
            results.append(outcome_result)
            if criterion.is_required and not actual_passed:
                any_effectively_required_failed = True

    failure_signal = None
    if any_effectively_required_failed:
        failure_signal = FailureSignal(
            source="validator", detail="a required, proven criterion failed", at=now()
        )

    return results, failure_signal, downgrade_notes


def score_certification_observations(state: RunState, ctx: NodeContext) -> list[CriterionResult]:
    """Score the applied skill's certification criteria against this artifact.

    Advisory only: never sets ``failure_signal`` or changes the caller's result
    (ADR-0003 amendment / specs §15.4).
    """

    if state.chosen is None or ctx.store is None:
        return []
    try:
        version = ctx.store.get_version(state.chosen.skill_id, state.chosen.version)
    except FileNotFoundError:
        return []
    observations: list[CriterionResult] = []
    for offset, criterion in enumerate(version.certification_criteria):
        if criterion.kind == "judge":
            continue
        op_seq = _CERT_OBS_OP_BASE + offset
        try:
            raw = ctx.op_once(
                op_seq, functools.partial(_score_criterion_dict, criterion, ctx)
            )
            observations.append(CriterionResult.model_validate(raw))
        except Exception as exc:  # noqa: BLE001 — observations must not fail the run
            observations.append(
                CriterionResult(
                    criterion_id=criterion.id,
                    kind=criterion.kind,  # type: ignore[arg-type]
                    passed=False,
                    weight=criterion.weight,
                    output_excerpt=f"certification observation error: {exc}"[:2000],
                    errored=True,
                )
            )
    return observations


def score_owned_criteria(
    criteria: Sequence[CriterionLike], ctx: NodeContext
) -> list[CriterionResult]:
    """Score caller-owned criteria against ``ctx.workdir`` (no advisory downgrade).

    Used by decomposition branches, which own a subset of the parent criteria and must
    not import the private scoring helper.
    """

    return [
        CriterionResult.model_validate(_score_criterion_dict(criterion, ctx))
        for criterion in criteria
    ]


class _FallbackExecutor:
    """Today's ``run_configured_command`` path, behind the executor port."""

    def __init__(self) -> None:
        from recertia.settings import load_settings

        self.config = load_settings().execution_config()

    def run(self, request: CommandRequest) -> CommandResult:
        from recertia.solver.container import run_configured_command
        from recertia.solver.sandbox import SandboxError

        try:
            proc = run_configured_command(
                request.command,
                workdir=request.workdir,
                timeout_s=int(request.timeout_s),
                limits=request.limits,
            )
        except SandboxError as exc:
            return CommandResult(returncode=126, stdout="", stderr=str(exc))
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


def criterion_runner() -> CriterionRunner:
    return CriterionRunner(_FallbackExecutor())


def _score_criterion_dict(criterion: CriterionLike, ctx: NodeContext) -> dict:
    return _score_criterion(criterion, ctx).model_dump(mode="json")


def _score_criterion(criterion: CriterionLike, ctx: NodeContext) -> CriterionResult:
    return criterion_runner().score(
        criterion,
        workdir=ctx.workdir,
        judge=JudgeContext(model=ctx.model, verifier_model=ctx.verifier_model),
    )
