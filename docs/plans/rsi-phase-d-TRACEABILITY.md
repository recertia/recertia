# TRACEABILITY

Plan: gap contract Phase D. Soak record. Flags stay default off. Default-on is not scheduled.

| Plan item | Code | Verification | Result | Status |
|---|---|---|---|---|
| 14 days or 30 promotion decisions, whichever is later | `window_state` | `test_window_waits_for_the_later_gate` | either gate alone leaves the soak open; both close it | PASS |
| Decisions outside the soak start do not count | `promotion_decision_times` | `test_decisions_outside_the_window_do_not_count` | 30 earlier promotes leave `decisions` at 0 | PASS |
| Abort when legacy would keep a skill prioritized would drop | `flipped_skills`, `soak_must_abort` | `test_ranking_flip_aborts_and_restores_without_authorizing_a_switch`, `test_below_threshold_is_not_a_flip` | flip aborts; a skill under the threshold does not | PASS |
| Abort restores the Phase C index and forces flags off | `evaluate_soak`, `rollback_dry_run`, `force_flags_off` | `test_ranking_flip_aborts_and_restores_without_authorizing_a_switch` | pointer back at generation 1, `sk_new` empty, opt-in names removed, one incident | PASS |
| Control-arm drop outside the pre-registered band | `should_rollback` | `test_control_arm_drop_aborts_inside_the_band_does_not` | 0.40 vs 0.50 band 0.05 aborts; the band edge does not | PASS |
| Written soak result, not a pass on this checkout | `scripts/record_rsi_soak.py`, `docs/ops/rsi-soak.json` | `test_checkout_record_is_not_a_pass` | `passed` false, `default_on` false | PASS |
| Completed window does not schedule default-on | `evaluate_soak` | `test_complete_window_does_not_schedule_default_on` | status `passed`, `default_on_scheduled` false, pointer unmoved | PASS |
| Unmeasured control arm cannot pass | `evaluate_soak` | `test_unmeasured_control_arm_cannot_pass` | open, reason `control_arm_unmeasured` | PASS |
| No flag default change | `Flags`, `rsi_phase_d.py` | `test_phase_d_does_not_change_flag_defaults` | defaults remain off; module does not assign a flag to `1` | PASS |
| Baseline promote before the window | `ensure_baseline_promote` | `test_open_soak_writes_the_baseline_and_locks_the_band` | one `baseline: true` row, generation 1, not counted as a decision | PASS |
| Noise band registered once, before the comparison | `register_noise_band` | `test_open_soak_writes_the_baseline_and_locks_the_band` | a second number raises; the current rate stays null | PASS |
| One skill row, or no open | `watch_skills`, `skills_from_sidecars`, `open_soak` | `test_open_soak_without_skills_does_not_start`, `test_open_soak_reads_skill_sidecars_and_keeps_the_baseline_on_abort` | empty list writes no soak row; a sidecar skill is watched; abort restores generation 1 | PASS |

**Notes:**

- A finding is not an authorization to switch retirement onto the prioritized score.
- Abort pops `RECERTIA_HYGIENE_ENABLED`, `RECERTIA_CALIBRATED_SCORING`, and `RECERTIA_PHASE4_RETIREMENT` from the process environment it is given. It does not set any of them.
- Replaying the same abort does not append a second incident. It does clear a flag that was turned back on.
- `docs/ops/rsi-soak.json` is the checkout result: no live window has been observed here. That file is not a soak exit pass.
- Default-on stays outside this phase.
