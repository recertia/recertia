# TRACEABILITY

Plan: `docs/plans` gap contract, Phase A and G9.

| Plan Item / Section | Code Location | Verification Command | Result | Status |
|---|---|---|---|---|
| Phase A script writes the capacity file and does not set flags | `scripts/measure_rsi_capacity.py:main` | `python3 -m pytest tests/unit/test_rsi_capacity.py -q --noconftest -o addopts=` | 10 passed | PASS |
| G9 `go` only when batch >= 50 and headroom >= 1.5 | `scripts/measure_rsi_capacity.py:measure` | same suite, `test_exact_gate_goes` and `test_headroom_below_one_point_five_does_not_go` | passed | PASS |
| Batch under 50 is not a go | `measure` | `test_forty_nine_candidates_do_not_go` | passed | PASS |
| Missing elapsed is not a measured 0s | `duration_s`, `measure` | `test_missing_elapsed_is_not_zero_seconds` | passed | PASS |
| Boolean is not an elapsed time | `duration_s` | `test_bool_elapsed_is_not_a_duration` | passed | PASS |
| Last 30 days only | `measure` | `test_records_older_than_thirty_days_are_outside_the_batch` | passed | PASS |
| Empty window is not a go and does not claim missing elapsed | `measure` | `test_empty_window_is_not_a_go` | passed | PASS |
| Waiver does not flip `go` and the script does not invent one | `measure`, `existing_waiver` | `test_waiver_does_not_flip_go`, `test_main_writes_only_the_report_and_keeps_a_waiver` | passed | PASS |
| In-repo promotion logs are outside the window | `load_history` | `test_repo_promotion_logs_are_not_a_go` | 10 records, batch 0, go false | PASS |
| No flag, no sandbox, no second write | `main` | `test_main_writes_only_the_report_and_keeps_a_waiver` | passed | PASS |
| Recorded baseline | `docs/ops/rsi-capacity.json` | `python3 scripts/measure_rsi_capacity.py` | no-go, batch_size 0, pool_headroom null | PASS |

**Full suite:** `python3 -m pytest tests/unit/test_rsi_capacity.py -q --noconftest -o addopts=` → 10 passed, 0 failed.

**Notes:**

- Phase B is not started. `go` is false and `slo_waiver` is null.
- The only in-repo distill history is `evals/golden/_promotion_logs` (10 records, all dated 2026-07-31). None fall in the 30 days before 2026-11-08. No file in the repo declares a sandbox pool size or a distill SLO, so headroom is null rather than 0.
- `pool_headroom` is free slots divided by `batch_size * p95_reexec_s / distill_slo_s`.
- This change does not import `recertia.rsi` into distill, nodes, or the worker.
