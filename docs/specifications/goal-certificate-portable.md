# Portable Goal Certificate Specification

**Status:** Proposed  
**Date:** 2026-10-26  
**Related:** Workplan WS3; existing Goal Certificate; ADR-0016 (local)

## Purpose

Additive, versioned extension of the existing hashed Goal Certificate for cross-domain use (repo-chore, research-synthesis, trading strategies). Separate checker remains isolated. No solver-writable fields.

## Requirements

- Hashed certificate preserved.
- Type-matched asks retained.
- Ask-Early history retained.
- Separate checker isolated from solvers.
- Schema changes additive and versioned.
- Cross-domain use (including trading) does not require structural graph changes.
- Isolation preserved; no plane collapse.

## T3 / Non-Modifiable Surfaces

Checker isolation, hashing, and certificate validation logic remain non-modifiable.

## Acceptance Criteria

- Additive only.
- Characterization tests cover schema compatibility.
- No write path that allows solvers to alter certificates.

## Non-Goals

No graph growth. No collapse of checker isolation.

## Cross-References

Existing Goal Certificate contracts, ADR-0005, measurement-and-scope.md.
