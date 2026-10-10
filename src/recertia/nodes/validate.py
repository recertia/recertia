"""``validate``: score locked criteria against the attempt (specs §4, §15.2, §26.3).

Supports ``command``, ``assertion``, ``schema``, ``metric``, and ``judge``. Judges run in
an isolated artifact+rubric context with ``context_hash`` recorded so isolation is testable.
"""

from __future__ import annotations

import functools
import json
import operator
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from contracts.criteria import CriterionResult, SkillCertificationCriterion, TaskCriterion
from contracts.failure import FailureSignal
from contracts.run import RunState
from recertia.nodes._util import now
from recertia.nodes.context import NodeContext, NodeOutcome
from recertia.validation.judge import assert_distinct_lenses, evaluate_judge

CriterionLike = TaskCriterion | SkillCertificationCriterion

_OPS = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
    "eq": operator.eq,
}


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


def _score_criterion_dict(criterion: CriterionLike, ctx: NodeContext) -> dict:
    return _score_criterion(criterion, ctx).model_dump(mode="json")


def _score_criterion(criterion: CriterionLike, ctx: NodeContext) -> CriterionResult:
    if criterion.kind == "command":
        return _run_command(criterion, ctx)
    if criterion.kind == "assertion":
        return _run_assertion(criterion, ctx)
    if criterion.kind == "schema":
        return _run_schema(criterion, ctx)
    if criterion.kind == "metric":
        return _run_metric(criterion, ctx)
    if criterion.kind == "judge":
        if not isinstance(criterion, TaskCriterion):
            raise ValueError(f"judge criterion {criterion.id!r} can only be scored as a TaskCriterion")
        if ctx.verifier_model is None:
            raise ValueError(f"judge criterion {criterion.id!r} requires an independent verifier model")
        if ctx.model is not None and ctx.verifier_model.shares_identity_with(ctx.model):
            raise ValueError("solver model cannot judge its own artifact")
        return evaluate_judge(criterion, workdir=ctx.workdir, model=ctx.verifier_model)
    raise ValueError(f"validate does not support kind={criterion.kind!r} for criterion {criterion.id!r}")


def _run_command(criterion: CriterionLike, ctx: NodeContext) -> CriterionResult:
    assert criterion.run is not None
    from recertia.solver.container import run_configured_command
    from recertia.solver.sandbox import SandboxError

    try:
        proc = run_configured_command(criterion.run, workdir=ctx.workdir, timeout_s=criterion.timeout_s)
        exit_code, output = proc.returncode, proc.stdout + proc.stderr
    except SandboxError as exc:
        exit_code, output = 126, str(exc)
    except subprocess.TimeoutExpired:
        exit_code, output = 124, f"timed out after {criterion.timeout_s}s"
    return CriterionResult(
        criterion_id=criterion.id,
        kind="command",
        passed=exit_code == criterion.expect_exit,
        weight=criterion.weight,
        exit_code=exit_code,
        output_excerpt=output[-2000:],
        duration_s=0.0,
    )


def _run_assertion(criterion: CriterionLike, ctx: NodeContext) -> CriterionResult:
    assert criterion.expr is not None
    from recertia.validation.assertions import UnsafeAssertionError, evaluate_assertion

    try:
        passed = evaluate_assertion(criterion.expr, workdir=ctx.workdir)
        excerpt = f"assertion {criterion.expr!r} => {passed}"
        errored = False
    except UnsafeAssertionError as exc:
        passed = False
        excerpt = f"assertion rejected: {exc}"
        errored = True
    except Exception as exc:  # noqa: BLE001
        passed = False
        excerpt = f"assertion error: {exc}"
        errored = True
    return CriterionResult(
        criterion_id=criterion.id,
        kind="assertion",
        passed=passed,
        weight=criterion.weight,
        output_excerpt=excerpt[:2000],
        errored=errored,
    )


class PathEscapeError(ValueError):
    """Raised when a criterion path escapes ``workdir``."""


def _resolve_path(workdir: Path, ref: str) -> Path:
    """Resolve ``ref`` strictly under ``workdir``; reject absolute paths and ``..`` escapes."""

    path = Path(ref)
    if path.is_absolute():
        raise PathEscapeError(f"absolute paths are not allowed: {ref!r}")
    root = workdir.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(f"path escapes workdir: {ref!r}") from exc
    return candidate


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_schema(criterion: CriterionLike, ctx: NodeContext) -> CriterionResult:
    assert criterion.target is not None and criterion.schema_ref is not None
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - core dependency missing from the install
        return CriterionResult(
            criterion_id=criterion.id,
            kind="schema",
            passed=False,
            weight=criterion.weight,
            output_excerpt=f"jsonschema unavailable: {exc}",
            errored=True,
        )
    try:
        target_path = _resolve_path(ctx.workdir, criterion.target)
        schema_path = _resolve_path(ctx.workdir, criterion.schema_ref)
    except PathEscapeError as exc:
        return CriterionResult(
            criterion_id=criterion.id,
            kind="schema",
            passed=False,
            weight=criterion.weight,
            output_excerpt=f"schema error: {exc}"[:2000],
            errored=True,
        )
    try:
        instance = _load_json(target_path)
        schema = _load_json(schema_path)
        jsonschema.validate(instance=instance, schema=schema)
        return CriterionResult(
            criterion_id=criterion.id,
            kind="schema",
            passed=True,
            weight=criterion.weight,
            output_excerpt=f"schema ok: {criterion.target} ⊨ {criterion.schema_ref}",
        )
    except Exception as exc:  # noqa: BLE001
        return CriterionResult(
            criterion_id=criterion.id,
            kind="schema",
            passed=False,
            weight=criterion.weight,
            output_excerpt=f"schema error: {exc}"[:2000],
            errored=not isinstance(exc, jsonschema.ValidationError),
        )


def _run_metric(criterion: CriterionLike, ctx: NodeContext) -> CriterionResult:
    assert criterion.metric is not None and criterion.op is not None and criterion.threshold is not None
    try:
        metrics_path = _resolve_path(ctx.workdir, "metrics.json")
    except PathEscapeError as exc:
        return CriterionResult(
            criterion_id=criterion.id,
            kind="metric",
            passed=False,
            weight=criterion.weight,
            output_excerpt=f"metric error: {exc}"[:2000],
            errored=True,
        )
    try:
        if not metrics_path.exists():
            raise FileNotFoundError("metrics.json missing in workdir")
        payload = _load_json(metrics_path)
        if not isinstance(payload, dict) or criterion.metric not in payload:
            raise KeyError(f"metric {criterion.metric!r} not present in metrics.json")
        raw = payload[criterion.metric]
        value = float(raw)
        cmp = _OPS[criterion.op]
        passed = bool(cmp(value, float(criterion.threshold)))
        excerpt = f"metric {criterion.metric}={value} {criterion.op} {criterion.threshold} => {passed}"
        return CriterionResult(
            criterion_id=criterion.id,
            kind="metric",
            passed=passed,
            weight=criterion.weight,
            output_excerpt=excerpt[:2000],
        )
    except Exception as exc:  # noqa: BLE001
        return CriterionResult(
            criterion_id=criterion.id,
            kind="metric",
            passed=False,
            weight=criterion.weight,
            output_excerpt=f"metric error: {exc}"[:2000],
            errored=True,
        )
