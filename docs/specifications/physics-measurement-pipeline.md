# Physics-Inspired Measurement Pipeline Specification

**Status:** Proposed (completed after harder synthesis)  
**Date:** 2026-10-26  
**Related:** Workplan WS4 (Tier B); particle-physics-quant-expert inputs; harder re-synthesis

## Purpose

Provide rigorous, falsifiable statistical procedures for causal_lift and implicit recovery, systematic uncertainties, and assumption resolution. Fully offline-capable on recorded or synthetic packets. Supports the measurement substrate; does not create new primary metrics.

## Procedures

1. **Monte Carlo Closure Tests**: Generate synthetic trajectories with known injected lift. Run full measurement pipeline. Acceptance: recovered lift lies within 2 standard errors of the injected value in ≥95% of N=100 synthetic runs. Golden fixture set required.

2. **Systematic Uncertainty Budget**: Categories include judge false-pass rate, model version, temperature, prompt variation, data selection. Template for documenting each contribution.

3. **Blinded Analysis Protocol for a1**: Freeze analysis code and selection criteria before unblinding. Document unblinding procedure. Human [F] required for unblinding decision. Fixture demonstrates that analysis code is frozen.

4. **Power Analysis & Sample-Size Calculator**: Default assumption: binomial proportion for implicit recovery rate, one-sided test, α=0.05, power=0.8.

Approximate required sample size (normal approximation):

\[
n = \left( \frac{z_{1-\alpha}\sqrt{p_0(1-p_0)} + z_{1-\beta}\sqrt{p_1(1-p_1)}}{p_1 - p_0} \right)^2
\]

where \( p_0 \) is baseline recovery, \( p_1 = p_0 + \delta \) (minimum detectable absolute lift), \( z \) from standard normal.

Clustered-by-week variant: inflate by design effect \( 1 + (m-1)\rho \) where m is average runs per week and ρ is intra-week correlation (default ρ=0.05 if unknown).

Empty/fixture weeks do not contribute to n. Rule: never claim support if Wilson interval includes 0.

5. **Pre-registration / Analysis Freeze Practice**: Any future claim about causal_lift or a1 must have an analysis plan (cuts, intervals, power calculation) frozen and hashed before looking at the data or before unblinding. This is a standing practice that supports the measurement substrate, not a one-time protocol. The frozen plan is part of the characterization fixture set.

## Data Ownership

Operates only on offline packets. Does not modify live measurement code.

## T3 / Non-Modifiable Surfaces

Measurement pipeline code and judge isolation remain non-modifiable.

## Acceptance Criteria

- Closure tests recover injected signal within 2σ in ≥95% of runs.
- Power calculator states assumptions and includes the formula above.
- Results labeled “not established” when intervals span zero or data insufficient.
- Control-arm divergence fixture fails if arms differ on model, temperature, or prompt.
- Pre-registration practice is stated and required for claims of support.

## Non-Goals

No modification of live measurement code in this sprint. No claims of support from sparse data. No new primary metrics.

## Cross-References

MetricReport, measurement-integrity.md, ADR-0005, harder re-synthesis.
