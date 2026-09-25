"""Pre-promotion applicability gate: environment, criterion alignment, contagion."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Protocol

from contracts.applicability import ApplicabilityReason, ApplicabilityReport, EnvironmentModel
from contracts.criteria import TaskCriterion
from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus


class _LoadedStore(Protocol):
    def iter_loaded(self) -> list[tuple[SkillVersion, SkillStatus, SkillStats]]: ...


_CONTAGION_LIFECYCLES = frozenset({"quarantined", "benched", "deprecated"})
_CONTAGION_COSINE = 0.97


def environment_model_from_registry(registry: object | None = None) -> EnvironmentModel:
    """Tools the current execution backend actually exposes.

    Prefer the run's ``ToolRuntime`` (or any object with ``names()``) when the caller
    has one; otherwise fall back to ``default_registry()``.
    """

    backend = os.environ.get("RECERTIA_EXECUTION_BACKEND", "container")
    if registry is not None and hasattr(registry, "names"):
        names = list(registry.names())
        return EnvironmentModel(tools=names, backend=backend)
    from recertia.solver.registry import default_registry

    reg = default_registry()
    names = list(reg.names()) if hasattr(reg, "names") else []
    return EnvironmentModel(tools=names, backend=backend)


def structural_hash(version: SkillVersion) -> str:
    """Stable hash of the skill's behavioural surface (tools, intents, preconditions)."""

    payload = {
        "task_class": version.task_class,
        "tools": [step.tool for step in version.steps],
        "intents": [step.intent.strip().lower() for step in version.steps],
        "preconditions": [(p.kind, p.value) for p in version.preconditions],
        "criteria": [(c.kind, c.run or c.expr or c.metric) for c in version.certification_criteria],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def check_applicability(
    version: SkillVersion,
    *,
    environment: EnvironmentModel | None = None,
    locked_criteria: list[TaskCriterion] | None = None,
    store: _LoadedStore | None = None,
) -> ApplicabilityReport:
    """Reject skills that name missing tools, un-evaluable claims, or rejected near-duplicates."""

    env = environment or environment_model_from_registry()
    reasons: list[ApplicabilityReason] = []
    digest = structural_hash(version)

    environment_ok = _environment_ok(version, env, reasons)
    criterion_ok = _criterion_ok(version, locked_criteria, reasons)
    contagion_ok = _contagion_ok(version, store, digest, reasons)

    return ApplicabilityReport(
        skill_id=version.skill_id,
        version=version.version,
        ok=environment_ok and criterion_ok and contagion_ok,
        environment_ok=environment_ok,
        criterion_ok=criterion_ok,
        contagion_ok=contagion_ok,
        reasons=reasons,
        structural_hash=digest,
    )


def refuse_if_inapplicable(
    version: SkillVersion,
    *,
    environment: EnvironmentModel | None = None,
    locked_criteria: list[TaskCriterion] | None = None,
    store: _LoadedStore | None = None,
    ledger: object | None = None,
) -> ApplicabilityReport:
    """Run the gate and record ``applicability_reject`` on the ledger when it fails."""

    report = check_applicability(
        version,
        environment=environment,
        locked_criteria=locked_criteria,
        store=store,
    )
    if report.ok:
        return report
    if ledger is not None and hasattr(ledger, "append"):
        ledger.append(
            actor="applicability-gate",
            action="applicability_reject",
            target=f"{version.skill_id}@v{version.version}",
            evidence=report.model_dump(mode="json"),
            at=datetime.now(timezone.utc),
        )
    return report


def _environment_ok(
    version: SkillVersion, env: EnvironmentModel, reasons: list[ApplicabilityReason]
) -> bool:
    available = set(env.tools)
    missing: list[str] = []
    for step in version.steps:
        if step.tool and step.tool not in available:
            missing.append(step.tool)
    for pre in version.preconditions:
        if pre.kind == "tool_available" and pre.value not in available:
            missing.append(pre.value)
    if not missing:
        return True
    unique = sorted(set(missing))
    reasons.append(
        ApplicabilityReason(
            check="environment",
            message=(
                f"skill references unavailable tool(s) {unique}; "
                f"environment exposes {sorted(available)}"
            ),
        )
    )
    return False


def _criterion_ok(
    version: SkillVersion,
    locked: list[TaskCriterion] | None,
    reasons: list[ApplicabilityReason],
) -> bool:
    if locked is None:
        if any(cert.kind != "judge" for cert in version.certification_criteria):
            return True
        reasons.append(
            ApplicabilityReason(
                check="criterion",
                message="skill has no non-judge certification criterion to evaluate success",
            )
        )
        return False
    required = [c for c in locked if c.is_required and c.kind != "judge"]
    if not required:
        return True
    for cert in version.certification_criteria:
        if cert.kind == "judge":
            continue
        for lc in required:
            if _exact_criterion_match(cert, lc):
                return True
    reasons.append(
        ApplicabilityReason(
            check="criterion",
            message=(
                "skill success claims cannot be evaluated by the locked task criteria "
                f"(locked kinds={sorted({c.kind for c in required})})"
            ),
        )
    )
    return False


def _exact_criterion_match(cert: object, locked: TaskCriterion) -> bool:
    """True when kind and the identifying field match exactly (no substring)."""

    if getattr(cert, "kind", None) != locked.kind:
        return False
    if locked.run is not None and getattr(cert, "run", None) == locked.run:
        return True
    if locked.expr is not None and getattr(cert, "expr", None) == locked.expr:
        return True
    if locked.metric is not None and getattr(cert, "metric", None) == locked.metric:
        return True
    return False


def _is_low_contribution(stats: SkillStats) -> bool:
    estimate = stats.contribution.estimate
    return estimate is not None and estimate <= 0


def _contagion_ok(
    version: SkillVersion,
    store: _LoadedStore | None,
    digest: str,
    reasons: list[ApplicabilityReason],
) -> bool:
    if store is None:
        return True
    ok = True
    for other, status, stats in store.iter_loaded():
        if other.skill_id == version.skill_id and other.version == version.version:
            continue
        if status.lifecycle not in _CONTAGION_LIFECYCLES and not _is_low_contribution(stats):
            continue
        if structural_hash(other) == digest:
            reasons.append(
                ApplicabilityReason(
                    check="contagion",
                    message=(
                        f"near-duplicate of {other.skill_id}@v{other.version} "
                        f"(lifecycle={status.lifecycle}, structural_hash={digest[:12]})"
                    ),
                )
            )
            ok = False
            continue
        if _advisory_embedding_near(version, other):
            reasons.append(
                ApplicabilityReason(
                    check="contagion",
                    message=(
                        f"advisory: embedding cosine near-duplicate of "
                        f"{other.skill_id}@v{other.version} (does not block promotion)"
                    ),
                )
            )
    return ok


def _advisory_embedding_near(version: SkillVersion, other: SkillVersion) -> bool:
    if version.task_class != other.task_class:
        return False
    if [step.tool for step in version.steps] != [step.tool for step in other.steps]:
        return False
    from recertia.retrieval.index import cosine, embed_text, skill_document

    sim = cosine(embed_text(skill_document(version)), embed_text(skill_document(other)))
    return sim >= _CONTAGION_COSINE

