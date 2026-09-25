"""Skill linting: structural + semantic profiles + uses-DAG + packaging (ADR-0015)."""

from __future__ import annotations

import re

from contracts.lint import LintFinding, LintReport, LintSeverity, lint_content_hash
from contracts.profiles import validate_approved_skill, validate_candidate_skill
from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus
from recertia.memory.procedural.store import SkillStore
from recertia.solver.claims import ClaimScheduler

_WHEN_CLAUSE = re.compile(r"\b(when|if|before|unless|only)\b", re.I)
_VAGUE = re.compile(
    r"\b(be careful|handle edge cases|edge cases|as appropriate|generally|"
    r"try to|make sure|etc\.?|and so on|do the right thing)\b",
    re.I,
)
_PROMOTION_DRAFT = frozenset({"draft", "candidate", "shadow"})


def lint_skill(
    version: SkillVersion,
    status: SkillStatus,
    stats: SkillStats | None = None,
    *,
    store: SkillStore | None = None,
    skip_if_hash_matches: bool = True,
) -> list[str]:
    """Return violation strings; empty means the skill is structurally + semantically clean."""

    report = lint_report(version, status, stats, store=store, skip_if_hash_matches=skip_if_hash_matches)
    return [f.message for f in report.errors]


def lint_report(
    version: SkillVersion,
    status: SkillStatus,
    stats: SkillStats | None = None,
    *,
    store: SkillStore | None = None,
    skip_if_hash_matches: bool = True,
) -> LintReport:
    digest = lint_content_hash(version)
    if (
        skip_if_hash_matches
        and version.hygiene.lint_content_hash
        and version.hygiene.lint_content_hash == digest
    ):
        return LintReport(findings=[], content_hash=digest)

    findings: list[LintFinding] = []
    stats = stats or SkillStats(skill_id=version.skill_id, version=version.version)

    if status.lifecycle in ("candidate", "shadow", "approved"):
        for message in validate_candidate_skill(version, status):
            findings.append(LintFinding(code="PROFILE", severity="error", message=message))
    if status.lifecycle == "approved":
        for message in validate_approved_skill(version, status, stats):
            findings.append(LintFinding(code="PROFILE", severity="error", message=message))

    findings.extend(_packaging_findings(version))
    findings.extend(_specificity_findings(version, status))

    if store is not None and version.uses:
        for message in _check_uses_resolve(version, store):
            findings.append(LintFinding(code="DAG", severity="error", message=message))
        for message in _check_uses_acyclic(version, store):
            findings.append(LintFinding(code="DAG", severity="error", message=message))
        for message in _check_composed_claims(version, store):
            findings.append(LintFinding(code="COMPOSE", severity="error", message=message))

    return LintReport(findings=findings, content_hash=digest)


def _specificity_severity(status: SkillStatus) -> LintSeverity:
    """ERROR for drafts entering the pipeline; WARNING for already-approved seeds."""

    return "error" if status.lifecycle in _PROMOTION_DRAFT else "warning"


def _specificity_findings(version: SkillVersion, status: SkillStatus) -> list[LintFinding]:
    findings: list[LintFinding] = []
    severity = _specificity_severity(status)
    if not version.preconditions:
        findings.append(
            LintFinding(
                code="SPEC",
                severity=severity,
                message="preconditions required: name the tools, files, or env the skill assumes",
            )
        )
    if not version.failure_modes:
        findings.append(
            LintFinding(
                code="SPEC",
                severity=severity,
                message="failure_modes required: each entry is condition → observed failure → recovery",
            )
        )
    else:
        for i, mode in enumerate(version.failure_modes):
            if len(mode.symptom.strip()) < 12 or len(mode.response.strip()) < 12:
                findings.append(
                    LintFinding(
                        code="SPEC",
                        severity=severity,
                        message=(
                            f"failure_modes[{i}] is too thin; name the observed failure "
                            "and the recovery action"
                        ),
                    )
                )
    blobs = [version.intent, version.title]
    blobs.extend(step.intent for step in version.steps)
    blobs.extend(mode.symptom for mode in version.failure_modes)
    blobs.extend(mode.response for mode in version.failure_modes)
    for blob in blobs:
        match = _VAGUE.search(blob)
        if match:
            findings.append(
                LintFinding(
                    code="VAGUE",
                    severity=severity,
                    message=f"vague language {match.group(0)!r} — name the condition and the recovery",
                )
            )
            break
    return findings


def _packaging_findings(version: SkillVersion) -> list[LintFinding]:
    findings: list[LintFinding] = []
    has_pre = bool(version.preconditions)
    has_when = bool(_WHEN_CLAUSE.search(version.intent))
    if not has_pre and not has_when:
        findings.append(
            LintFinding(
                code="R1.3",
                severity="warning",
                message="preconditions missing and intent has no when/if clause",
            )
        )
    slug_as_title = version.skill_id.replace("-", " ")
    if version.title.lower() == slug_as_title:
        findings.append(
            LintFinding(code="R1.4", severity="warning", message="title is the skill_id slug")
        )
    for step in version.steps:
        if step.intent.replace("-", "_").lower() == step.id:
            findings.append(
                LintFinding(
                    code="R2.4",
                    severity="error",
                    message=f"step {step.id!r} intent equals the step id",
                )
            )
        if len(step.intent) > 2048:
            findings.append(
                LintFinding(code="R3.1", severity="warning", message=f"step {step.id!r} intent > 2KiB")
            )
    if version.hygiene.secret_scan == "failed":
        findings.append(LintFinding(code="R5", severity="error", message="secret scan failed"))
    if all(c.kind == "judge" for c in version.certification_criteria):
        findings.append(
            LintFinding(code="CRIT", severity="error", message="no non-judge certification criterion")
        )
    return findings


def _check_uses_resolve(version: SkillVersion, store: SkillStore) -> list[str]:
    violations: list[str] = []
    for use in version.uses:
        try:
            store.get_version(use.skill_id, use.version)
        except FileNotFoundError:
            violations.append(
                f"uses entry {use.skill_id}@v{use.version} does not exist in the skill store"
            )
    return violations


def _check_uses_acyclic(version: SkillVersion, store: SkillStore, *, max_depth: int = 3) -> list[str]:
    """Walk the ``uses`` DAG; reject cycles and depth > 3 (specs §2.4, §14)."""

    violations: list[str] = []

    def walk(skill_id: str, ver: int, path: list[tuple[str, int]], depth: int) -> None:
        key = (skill_id, ver)
        if key in path:
            cycle = " -> ".join(f"{s}@v{v}" for s, v in path + [key])
            violations.append(f"uses graph contains a cycle: {cycle}")
            return
        if depth > max_depth:
            violations.append(
                f"uses depth exceeds {max_depth} at {skill_id}@v{ver} "
                f"(path: {' -> '.join(f'{s}@v{v}' for s, v in path)})"
            )
            return
        try:
            child = store.get_version(skill_id, ver)
        except FileNotFoundError:
            return
        for use in child.uses:
            walk(use.skill_id, use.version, path + [key], depth + 1)

    for use in version.uses:
        walk(use.skill_id, use.version, [(version.skill_id, version.version)], 1)
    return violations


def _check_composed_claims(version: SkillVersion, store: SkillStore) -> list[str]:
    """P6: sibling uses with overlapping write/exclusive claims and no serialising edge."""

    if len(version.uses) < 2:
        return []
    claimed: dict[tuple[str, str], list[str]] = {}
    for use in version.uses:
        try:
            child = store.get_version(use.skill_id, use.version)
        except FileNotFoundError:
            continue
        for step in child.steps:
            for claim in step.resources:
                if claim.mode in ("write", "exclusive"):
                    claimed.setdefault((claim.kind, claim.id), []).append(
                        f"{use.skill_id}@v{use.version}"
                    )
    violations: list[str] = []
    for (kind, cid), holders in claimed.items():
        unique = sorted(set(holders))
        if len(unique) < 2:
            continue
        # A parent-level input_binding would serialise; we only have uses pins here.
        _ = ClaimScheduler.conflicts_with
        violations.append(
            f"composed skills {unique} share undeclared {kind}:{cid} write/exclusive claim"
        )
    return violations


def lint_store(store: SkillStore) -> dict[str, list[str]]:
    """Lint every version in the store; returns ``{skill_id@version: [violations]}`` (empty ok)."""

    report: dict[str, list[str]] = {}
    for version, status, stats in store.iter_loaded():
        key = f"{version.skill_id}@v{version.version}"
        report[key] = lint_skill(version, status, stats, store=store)
    return report
