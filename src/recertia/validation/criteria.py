"""One scorer for locked criteria. Command criteria run through ``CommandExecutor``."""

from __future__ import annotations

import json
import operator
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts.criteria import CriterionResult, SkillCertificationCriterion, TaskCriterion
from recertia.errors import ExecutionUnavailable
from recertia.ports.execution import CommandExecutor, CommandRequest

CriterionLike = TaskCriterion | SkillCertificationCriterion

_OPS = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
    "eq": operator.eq,
}


@dataclass
class JudgeContext:
    """Verifier identity for a judge criterion. Absent for mechanical kinds."""

    model: Any = None
    verifier_model: Any = None


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


class CriterionRunner:
    def __init__(self, executor: CommandExecutor) -> None:
        self.executor = executor

    def score(
        self,
        criterion: CriterionLike,
        *,
        workdir: Path,
        judge: JudgeContext | None = None,
    ) -> CriterionResult:
        if criterion.kind == "command":
            return self._run_command(criterion, workdir)
        if criterion.kind == "assertion":
            return _run_assertion(criterion, workdir)
        if criterion.kind == "schema":
            return _run_schema(criterion, workdir)
        if criterion.kind == "metric":
            return _run_metric(criterion, workdir)
        if criterion.kind == "judge":
            return _run_judge(criterion, workdir, judge or JudgeContext())
        raise ValueError(
            f"validate does not support kind={criterion.kind!r} for criterion {criterion.id!r}"
        )

    def score_all(
        self,
        criteria: list[CriterionLike] | tuple[CriterionLike, ...],
        *,
        workdir: Path,
        judge: JudgeContext | None = None,
    ) -> list[CriterionResult]:
        return [self.score(criterion, workdir=workdir, judge=judge) for criterion in criteria]

    def _run_command(self, criterion: CriterionLike, workdir: Path) -> CriterionResult:
        assert criterion.run is not None
        try:
            proc = self.executor.run(
                CommandRequest(
                    command=criterion.run,
                    workdir=workdir,
                    timeout_s=float(criterion.timeout_s),
                    origin="criterion",
                )
            )
            exit_code, output = proc.returncode, proc.stdout + proc.stderr
        except ExecutionUnavailable as exc:
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


def _run_assertion(criterion: CriterionLike, workdir: Path) -> CriterionResult:
    assert criterion.expr is not None
    from recertia.validation.assertions import UnsafeAssertionError, evaluate_assertion

    try:
        passed = evaluate_assertion(criterion.expr, workdir=workdir)
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


def _run_schema(criterion: CriterionLike, workdir: Path) -> CriterionResult:
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
        target_path = _resolve_path(workdir, criterion.target)
        schema_path = _resolve_path(workdir, criterion.schema_ref)
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


def _run_metric(criterion: CriterionLike, workdir: Path) -> CriterionResult:
    assert criterion.metric is not None and criterion.op is not None and criterion.threshold is not None
    try:
        metrics_path = _resolve_path(workdir, "metrics.json")
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


def _run_judge(criterion: CriterionLike, workdir: Path, judge: JudgeContext) -> CriterionResult:
    from recertia.validation.judge import evaluate_judge

    if not isinstance(criterion, TaskCriterion):
        raise ValueError(f"judge criterion {criterion.id!r} can only be scored as a TaskCriterion")
    if judge.verifier_model is None:
        raise ValueError(f"judge criterion {criterion.id!r} requires an independent verifier model")
    if judge.model is not None and judge.verifier_model.shares_identity_with(judge.model):
        raise ValueError("solver model cannot judge its own artifact")
    return evaluate_judge(criterion, workdir=workdir, model=judge.verifier_model)
