# TRACEABILITY

Plan: [terminal-rsi-leverage.md](terminal-rsi-leverage.md)

| Plan Item / Section | Code Location | Verification Command | Result | Status |
|---------------------|---------------|----------------------|--------|--------|
| R1 fresh execution before accept | `src/recertia/rsi/hygiene.py:HygieneGate._execute` | `python -m unittest tests.unit.test_rsi_plan.HygieneSuite.test_accepts_only_after_fresh_strict_execution` | ok | PASS |
| R2 leakage hard reject, span hash only | `src/recertia/rsi/leakage.py:screen_leakage` | `python -m unittest tests.unit.test_rsi_plan.LeakageSuite` | 2 tests ok | PASS |
| R3 negatives off the active cap | `src/recertia/rsi/hygiene.py:_reject`, `src/recertia/rsi/world.py:active_cap_occupancy` | `python -m unittest tests.unit.test_rsi_plan.HygieneSuite.test_leakage_and_verifier_fail_are_negative_and_off_cap` | ok | PASS |
| R4 screened artifact is not a skill | `src/recertia/rsi/pipeline.py:distill`, `src/recertia/rsi/pipeline.py:retrieve` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_screened_artifact_is_not_retrievable_until_promote` | ok | PASS |
| R5 / §8.4 prioritized vs dense score | `src/recertia/rsi/scoring.py:success_prioritized_reward` | `python -m unittest tests.unit.test_rsi_plan.ScoringSuite.test_full_family_failure_cannot_outrank_strict_success` | ok | PASS |
| R6 lift authorizes promotion, scores do not | `src/recertia/rsi/pipeline.py:promote` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_promotion_requires_manifest_and_lift_not_score` | ok | PASS |
| R7 failure-share drop ≥ 15pp | `src/recertia/rsi/scoring.py:accept_constants` | `python -m unittest tests.unit.test_rsi_plan.ScoringSuite.test_failure_share_drops_at_least_15_points_and_order_holds` | ok (drop 0.444) | PASS |
| R8 turn-boundary credit, observations zero | `src/recertia/rsi/attribution.py:attribute` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_turn_credit_decays_and_ignores_observations` | ok | PASS |
| R9 recalibration blocks retrieval eligibility | `src/recertia/rsi/pipeline.py:run_recalibration`, `src/recertia/rsi/pipeline.py:promote` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_duration_breach_blocks_and_does_not_move_index` | ok | PASS |
| R10 prior index stays live until commit | `src/recertia/rsi/pipeline.py:RetrievalIndex` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_prepare_without_commit_keeps_prior_generation` | ok | PASS |
| R11 ledger replay | `src/recertia/rsi/pipeline.py:replay`, `src/recertia/rsi/ledger.py:Ledger` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_promote_retrieve_and_replay` | ok | PASS |
| R12 control-arm regression rolls back | `src/recertia/rsi/pipeline.py:should_rollback`, `src/recertia/rsi/pipeline.py:rollback` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_rollback_restores_baseline_generation` | ok | PASS |
| R13 idempotent hygiene and single-flight | `src/recertia/rsi/hygiene.py:HygieneGate.run` | `python -m unittest tests.unit.test_rsi_plan.HygieneSuite.test_idempotency_does_not_reexecute tests.unit.test_rsi_plan.HygieneSuite.test_single_flight_executes_once tests.unit.test_rsi_plan.HygieneSuite.test_second_key_is_duplicate_without_second_artifact` | ok | PASS |
| R14 20 leak reject + 20 clean accept | `tests/unit/test_rsi_plan.py:LeakageSuite` | `python -m unittest tests.unit.test_rsi_plan.LeakageSuite` | ok | PASS |
| R15 no T3 import | `tests/unit/test_rsi_import_boundary.py` | `python -m unittest tests.unit.test_rsi_import_boundary` | ok | PASS |
| §8.1 payload cap, body not on ledger | `hygiene.py` reject path | `python -m unittest tests.unit.test_rsi_plan.HygieneSuite.test_payload_over_cap_stores_no_body` | ok | PASS |
| §9.3 recalibration idempotent, no index move | `src/recertia/rsi/pipeline.py:run_recalibration` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_recalibration_is_idempotent` | ok | PASS |
| §10 T2 human gate for constants | `src/recertia/rsi/world.py:PolicyStore.put_constants` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_promote_retrieve_and_replay` | ok | PASS |
| §11 sidecar not rewritten; console pair | `src/recertia/rsi/pipeline.py:write_sidecar`, `src/recertia/rsi/pipeline.py:snapshot_views` | covered by promote/replay test | ok | PASS |
| §12 capacity gate, sampling stays illegal | `src/recertia/rsi/pipeline.py:capacity_report` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_capacity_gate_blocks_phase1_when_headroom_is_low` | ok | PASS |
| §13 flags default off | `src/recertia/rsi/world.py:Flags` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_flags_default_off_and_legacy_import_skips_sandbox` | ok | PASS |
| S3 review queue cannot promote | `src/recertia/rsi/pipeline.py:promote_from_review` | `python -m unittest tests.unit.test_rsi_plan.PipelineSuite.test_review_queue_cannot_promote` | ok | PASS |
| Phase 2 soak abort on ranking flip | `src/recertia/rsi/scoring.py:soak_must_abort` | `python -m unittest tests.unit.test_rsi_plan.ScoringSuite.test_retirement_keeps_legacy_score_until_phase4` | ok | PASS |
| Phase 0 calibration not activated | `scripts/calibrate_rsi.py` | `python scripts/calibrate_rsi.py` | accepted true, activated false | PASS |
| ADR-001..004 | hygiene, scoring, recalibration, retrieve | suite above | — | PASS |

**Full suite:** `python -m pytest tests/unit/test_rsi_plan.py tests/unit/test_rsi_import_boundary.py -q` → 26 passed, 0 failed.

**Notes:**

- Additive package `src/recertia/rsi`. Not wired into live distill, the episodic store, or the OCI sandbox. The hygiene gate takes an injected executor and forwards only the public prefix.
- `RECERTIA_HYGIENE_ENABLED`, `RECERTIA_CALIBRATED_SCORING`, and `RECERTIA_PHASE4_RETIREMENT` default off. With hygiene off, `import_trajectory` writes `legacy_import` and does not enter the sandbox.
- Phase 4 deletion of the legacy score branch is not in this change. The spec removes it only after a real soak.
- `scripts/calibrate_rsi.py` writes `candidate_constants.json` with `"activated": false` and does not call `install_constants`. That file is not committed. Installation requires `PolicyStore.approve_gate`.
- No weight update, PPO, or T3 write path exists. `t3_config.set_ablation_rate` raises `PermissionError`.
- CHANGELOG.md is intentionally untouched in this PR.
