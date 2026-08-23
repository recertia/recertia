"""Goal objects: structured primary task input (Variant B).

A Goal is the preferred entry point for a run. It declares desired outcomes and hard
constraints, then compiles deterministically into ``list[TaskCriterion]`` at ``intake``.
Natural-language ``context`` is optional supporting text and never the sole success
contract.

See docs/specifications (criteria integrity) and the technical plan for Variant B.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.criteria import TaskCriterion

DesiredKind = Literal[
    "file_exists",
    "file_contains",
    "command",
    "assertion",
    "schema",
    "metric",
    "judge",
]

ConstraintKind = Literal[
    "must_not_modify",
    "must_pass_command",
    "budget_ceiling",
    "no_external_effects",
]


class DesiredState(BaseModel):
    """One atomic, checkable desired outcome."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: DesiredKind
    path: str | None = None
    pattern: str | None = None
    run: str | None = None
    expect_exit: int = 0
    expr: str | None = None
    target: str | None = None
    schema_ref: str | None = None
    metric: str | None = None
    op: Literal["lt", "lte", "gt", "gte", "eq"] | None = None
    threshold: float | None = None
    rubric: str | None = None
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    timeout_s: int = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def _kind_requires_fields(self) -> "DesiredState":
        if self.kind == "file_exists" and not self.path:
            raise ValueError(f"DesiredState {self.id!r} kind=file_exists requires path")
        if self.kind == "file_contains" and (not self.path or not self.pattern):
            raise ValueError(
                f"DesiredState {self.id!r} kind=file_contains requires path and pattern"
            )
        if self.kind == "command" and not self.run:
            raise ValueError(f"DesiredState {self.id!r} kind=command requires run")
        if self.kind == "assertion" and not self.expr:
            raise ValueError(f"DesiredState {self.id!r} kind=assertion requires expr")
        if self.kind == "schema" and (not self.target or not self.schema_ref):
            raise ValueError(
                f"DesiredState {self.id!r} kind=schema requires target and schema_ref"
            )
        if self.kind == "metric" and (
            self.metric is None or self.op is None or self.threshold is None
        ):
            raise ValueError(
                f"DesiredState {self.id!r} kind=metric requires metric, op, threshold"
            )
        if self.kind == "judge" and not self.rubric:
            raise ValueError(f"DesiredState {self.id!r} kind=judge requires rubric")
        return self


class Constraint(BaseModel):
    """Hard limits that must not be violated."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: ConstraintKind
    value: str | float | list[str]
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class Goal(BaseModel):
    """Primary task input. Compiles to TaskCriterion[] at intake."""

    model_config = ConfigDict(extra="forbid")

    goal_id: str | None = None
    desired: list[DesiredState] = Field(min_length=1)
    constraints: list[Constraint] = Field(default_factory=list)
    context: str | None = Field(
        default=None,
        description=(
            "Optional natural-language context / human label. "
            "Never the sole success contract."
        ),
    )
    task_class: str | None = None
    preferences: dict[str, str] = Field(
        default_factory=dict,
        description="Soft guidance (style, verbosity, preferred tools). Advisory only.",
    )
    strategy_hint: Literal["abstain", "portfolio", "decomposition"] | None = None
    mea_opt_in: bool = Field(
        default=False,
        description=(
            "Per-Goal MEA layer of three-layer activation "
            "(policy.mea_enabled + this flag + Task.execution_strategy). Default false."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one_hard_criterion(self) -> "Goal":
        hard = [d for d in self.desired if d.weight >= 1.0 and d.kind != "judge"]
        if not hard:
            raise ValueError(
                "Goal must contain ≥1 required non-judge DesiredState "
                "(machine-checkable success contract)"
            )
        return self


def compile_goal(
    goal: Goal,
    *,
    source: Literal["caller", "task_class_template", "critic"] = "caller",
) -> list[TaskCriterion]:
    """Pure, deterministic compilation of a Goal into TaskCriterion[].

    Budget and no_external_effects constraints are *not* turned into criteria; they are
    enforced by budget accounting and tool-runtime policy respectively.
    """
    criteria: list[TaskCriterion] = []

    for d in goal.desired:
        criteria.append(_desired_to_criterion(d, source=source))

    for c in goal.constraints:
        if c.kind in ("budget_ceiling", "no_external_effects"):
            continue
        criteria.append(_constraint_to_criterion(c, source=source))

    return criteria


def _desired_to_criterion(
    d: DesiredState,
    *,
    source: Literal["caller", "task_class_template", "critic"],
) -> TaskCriterion:
    if d.kind == "file_exists":
        return TaskCriterion(
            id=d.id,
            kind="command",
            run=f"test -f {d.path}",
            expect_exit=0,
            weight=d.weight,
            timeout_s=d.timeout_s,
            source=source,
            preregistered=True,
        )
    if d.kind == "file_contains":
        # Escape single quotes for shell safety in the generated check.
        pat = (d.pattern or "").replace("'", "'\\''")
        path = d.path or ""
        return TaskCriterion(
            id=d.id,
            kind="command",
            run=f"grep -qE '{pat}' {path}",
            expect_exit=0,
            weight=d.weight,
            timeout_s=d.timeout_s,
            source=source,
            preregistered=True,
        )
    if d.kind == "command":
        return TaskCriterion(
            id=d.id,
            kind="command",
            run=d.run,
            expect_exit=d.expect_exit,
            weight=d.weight,
            timeout_s=d.timeout_s,
            source=source,
            preregistered=True,
        )
    if d.kind == "assertion":
        return TaskCriterion(
            id=d.id,
            kind="assertion",
            expr=d.expr,
            weight=d.weight,
            timeout_s=d.timeout_s,
            source=source,
            preregistered=True,
        )
    if d.kind == "schema":
        return TaskCriterion(
            id=d.id,
            kind="schema",
            target=d.target,
            schema_ref=d.schema_ref,
            weight=d.weight,
            timeout_s=d.timeout_s,
            source=source,
            preregistered=True,
        )
    if d.kind == "metric":
        return TaskCriterion(
            id=d.id,
            kind="metric",
            metric=d.metric,
            op=d.op,
            threshold=d.threshold,
            weight=d.weight,
            timeout_s=d.timeout_s,
            source=source,
            preregistered=True,
        )
    # judge
    return TaskCriterion(
        id=d.id,
        kind="judge",
        rubric=d.rubric,
        isolation="fresh_context",
        weight=d.weight,
        timeout_s=d.timeout_s,
        source=source,
        preregistered=True,
    )


def _constraint_to_criterion(
    c: Constraint,
    *,
    source: Literal["caller", "task_class_template", "critic"],
) -> TaskCriterion:
    if c.kind == "must_pass_command":
        if not isinstance(c.value, str):
            raise ValueError(
                f"Constraint {c.id!r} kind=must_pass_command requires str value"
            )
        return TaskCriterion(
            id=c.id,
            kind="command",
            run=c.value,
            expect_exit=0,
            weight=c.weight,
            source=source,
            preregistered=True,
        )
    if c.kind == "must_not_modify":
        paths: list[str]
        if isinstance(c.value, list):
            paths = [str(p) for p in c.value]
        elif isinstance(c.value, str):
            paths = [c.value]
        else:
            raise ValueError(
                f"Constraint {c.id!r} kind=must_not_modify requires str or list[str]"
            )
        # Pre-image hashes are taken at intake; the check is a placeholder command that
        # the runtime can replace with a real snapshot diff. For the contract layer we
        # emit a command criterion that fails if any listed path is missing (conservative).
        checks = " && ".join(f"test -e {p}" for p in paths) if paths else "true"
        return TaskCriterion(
            id=c.id,
            kind="command",
            run=checks,
            expect_exit=0,
            weight=c.weight,
            source=source,
            preregistered=True,
        )
    raise ValueError(f"Constraint {c.id!r} kind={c.kind!r} is not compilable to a criterion")
