# TRACEABILITY

Plan: gap contract Phase C. G10–G16. Flags stay default off. Retirement stays on the legacy score.

| Plan item | Code | Verification | Result | Status |
|---|---|---|---|---|
| G10 golden corpus, constants not activated | `scripts/calibrate_rsi_golden.py`, `golden_report` | `test_golden_report_is_not_activated_and_does_not_install` | activated false, approved_by empty | PASS |
| G10 0.15 failure-share gate, Spearman unchanged | `accept_constants` | `test_failure_share_gate_needs_fifteen_points` | synthetic gate passes; golden drop is 0 because every log is a success, so `accepted` stays false | PASS |
| G11 sidecar, retirement ignores prioritized | `write_score_sidecar`, `retirement_mass` | `test_sidecar_roundtrip_and_retirement_ignores_prioritized` | flipped prioritized ranking, same kept set | PASS |
| G12 console pair and lift label | `console_snapshot`, tower cards | `test_console_shows_both_numbers_and_labels_a_liftless_move` | process score, strict completion, failure share, lift; label `not an improvement` | PASS |
| G13 golden validators plus stratified sample, no solve | `production_recalibrate` | `test_duration_breach_does_not_move_the_pointer`, `test_bad_sample_blocks_and_a_clean_pass_commits` | duration at 5% leaves the pointer; bad outcome blocks | PASS |
| G14 prepare / commit / abort on the production ledger | `DurableIndex` | `test_crash_between_prepare_and_commit_keeps_the_prior_generation` | restart sees generation 0, then abort | PASS |
| G15 lineage ledger row, no skill-schema break | `write_lineage` | `test_lineage_is_a_ledger_row_and_golden_skills_still_validate` | `add-editorconfig` still validates | PASS |
| G16 rollback dry-run on `HashChainLedger` | `rollback_dry_run` | `test_rollback_dry_run_on_the_production_ledger` | pointer restored, `sk_new` retrieves empty, receipts kept, incident appended | PASS |
| Flags and phase 4 stay off | `src/recertia/ops/rsi_phase_c.py` | `test_phase_c_does_not_set_flags_or_phase4` | the three flag names are absent | PASS |

**Notes:**

- The ten in-repo promotion logs are all `passed: true` and `terminal: solved`. Failure share does not drop by 0.15 on that corpus. The candidate file stays `activated: false`. That is not a T2 approval.
- `gamma_turn` without an `approved_by` record is 0.95 and `constants_version` is `unset`.
- The live index pointer is the last `rsi_index_committed` or `rsi_index_restored` row. A trailing `rsi_index_prepared` is not live. Restart aborts it unless a passed manifest is supplied.
- Phase D is not in this change. No flag default changes.
