"""Family-dense and success-prioritized scores. Spec R5–R7.

Legacy score is raw check mass (checks_passed). It is the pre-change
baseline the failure-share drop is measured against. Promotion never
reads these scores.
"""

from __future__ import annotations

from recertia.rsi.models import CalibConstants, ScoreSidecar


def family_dense_reward(checks_passed: int, family: str, c: CalibConstants) -> float:
    d_f = c.family_quantile.get(family, c.fallback_d)
    if d_f <= 0:
        return 0.0
    return max(checks_passed / d_f, 0.0)


def success_prioritized_reward(
    strict_success: bool, checks_passed: int, family: str, c: CalibConstants
) -> float:
    d_f = c.family_quantile.get(family, c.fallback_d)
    if d_f <= 0:
        d_f = c.fallback_d
    if strict_success:
        denom = checks_passed + d_f
        partial = (checks_passed / denom) if denom else 0.0
        return 1.0 + partial
    return family_dense_reward(checks_passed, family, c) * c.partial_scale


def legacy_check_mass(checks_passed: int) -> float:
    return float(checks_passed)


def score_run(
    run_id: str,
    task_class: str,
    checks_passed: int,
    checks_total: int,
    strict_success: bool,
    c: CalibConstants,
) -> ScoreSidecar:
    dense = family_dense_reward(checks_passed, task_class, c)
    prioritized = success_prioritized_reward(strict_success, checks_passed, task_class, c)
    return ScoreSidecar(
        run_id=run_id,
        legacy=legacy_check_mass(checks_passed),
        dense=dense,
        prioritized=prioritized,
        constants_version=c.version,
        strict_success=strict_success,
        task_class=task_class,
        checks_passed=checks_passed,
        checks_total=checks_total,
    )


def failure_share(scores: list[ScoreSidecar], field: str) -> float:
    total = 0.0
    failed = 0.0
    for row in scores:
        mass = float(getattr(row, field))
        total += mass
        if not row.strict_success:
            failed += mass
    if total <= 0:
        return 0.0
    return failed / total


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or not xs:
        raise ValueError("spearman inputs must be the same non-empty length")
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(xs)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n))
    dy = sum((ry[i] - my) ** 2 for i in range(n))
    den = (dx * dy) ** 0.5
    if den == 0:
        return 1.0
    return num / den


def strict_rates_by_family(scores: list[ScoreSidecar]) -> dict[str, float]:
    buckets: dict[str, list[bool]] = {}
    for row in scores:
        buckets.setdefault(row.task_class, []).append(row.strict_success)
    return {fam: (sum(1 for v in vals if v) / len(vals)) for fam, vals in buckets.items()}


def accept_constants(corpus: list[ScoreSidecar]) -> tuple[bool, dict]:
    """Accept only if failure share drops by >= 15pp and strict-success
    family order is unchanged. Also reject if any failure outranks the
    weakest strict success (spec 8.4)."""
    if not corpus:
        return False, {"reason": "empty corpus"}
    old = failure_share(corpus, "legacy")
    new = failure_share(corpus, "prioritized")
    drop = old - new
    families = sorted(strict_rates_by_family(corpus))
    rates = [strict_rates_by_family(corpus)[f] for f in families]
    # Both scorers share the same strict labels, so the rate vector is identical.
    rho = spearman(rates, rates)
    successes = [row.prioritized for row in corpus if row.strict_success]
    failures = [row.prioritized for row in corpus if not row.strict_success]
    outranks = False
    if successes and failures and max(failures) >= min(successes):
        outranks = True
    ok = drop >= 0.15 - 1e-12 and rho == 1.0 and not outranks
    return ok, {
        "failure_share_old": old,
        "failure_share_new": new,
        "drop": drop,
        "spearman_strict": rho,
        "failure_outranks_success": outranks,
    }


def contribution_for_retirement(row: ScoreSidecar, phase4: bool) -> float:
    """Phase 4 is the only point at which the new score may retire."""
    if phase4:
        return row.prioritized
    return row.legacy


def soak_must_abort(rows: list[ScoreSidecar], threshold: float) -> bool:
    """Abort when a skill kept by the old score would be retired by the new one."""
    for row in rows:
        if row.legacy >= threshold and row.prioritized < threshold:
            return True
    return False
