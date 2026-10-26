# Physics-Inspired Measurement Pipeline Specification

**Status:** Proposed  
**Date:** 2026-10-26  
**Related:** Workplan WS4; particle-physics-quant-expert inputs

## Purpose

Provide rigorous, falsifiable statistical procedures for causal_lift and implicit recovery, systematic uncertainties, and assumption resolution. Fully offline-capable on recorded or synthetic packets.

## Procedures

1. **Monte Carlo Closure Tests**: Generate synthetic trajectories with known injected lift. Run full measurement pipeline. Acceptance: recovered lift consistent with injected value within expected uncertainty.

2. **Systematic Uncertainty Budget**: Categories include judge false-pass rate, model version, temperature, prompt variation, data selection. Template for documenting each contribution.

3. **Blinded Analysis Protocol for a1**: Freeze analysis code and selection criteria before unblinding. Document unblinding procedure. Human [F] required for unblinding decision.

4. **Power Analysis & Sample-Size Calculator**: Formula for required N to detect a minimum effect size on implicit recovery, given baseline variance and desired power (e.g., 80%). Explicit statistical assumptions (binomial or clustered). Rule: do not claim support until interval excludes zero. Empty or fixture weeks do not count toward the sample.

## Data Ownership

Operates only on offline packets. Does not modify live measurement code.

## T3 / Non-Modifiable Surfaces

Measurement pipeline code and judge isolation remain non-modifiable.

## Acceptance Criteria

- Closure tests recover injected signal within expected uncertainty.
- Power calculator states assumptions.
- Results labeled “not established” when intervals span zero or data insufficient.

## Non-Goals

No modification of live measurement code in this sprint. No claims of support from sparse data.

## Cross-References

MetricReport, measurement-integrity.md, ADR-0005.
