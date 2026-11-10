# TRACEABILITY

Plan: gap contract Phase B. G1–G8 and G17. Flags stay default off.

| Plan item | Code | Verification | Result | Status |
|---|---|---|---|---|
| G1 dedup key includes payload sha256 | `src/recertia/rsi/hygiene.py` | `tests/unit/test_rsi_plan.py` duplicate and distinct-payload tests | passed | PASS |
| G2 container executor, missing budget is sandbox_error, no `run_sandboxed` | `src/recertia/nodes/rsi_bridge.py` | `test_missing_budget_unknown_policy_and_private_bag_do_not_start`, import-boundary name scan | passed | PASS |
| G3 public prefix only | `production_executor` | same test, private bag and host path raise before the runner | passed | PASS |
| G4 durable episodic object, ledger receipt, restart replay | `run_durable_hygiene` | `test_restart_replays_idempotency_without_a_second_run` | passed | PASS |
| G5 flag default off on both distill writers | `nodes/distill.py`, `app/library.py` | `test_flag_off_does_not_call_the_runner`; flag-off branch is the previous library body | passed | PASS |
| G6 no candidate on verifier_fail or leakage | `live_hygiene_block`, `distill_imported` | `test_flag_on_verifier_fail_does_not_author`, `test_library_flag_on_rejects_before_write_candidate` | passed | PASS |
| G7 ingest does not re-execute | `LibraryService.import_trajectory` unchanged | `test_import_trajectory_does_not_call_the_bridge`, `tests/unit/test_trajectory_import_cli.py` | passed | PASS |
| G8 retrieve drops `scr_` ids | `nodes/retrieve.py` | `test_retrieve_misses_a_screened_artifact` | passed | PASS |
| G17 `rsi` imports no solver, ledger, memory, distill, governance, or t3 | `tests/unit/test_rsi_import_boundary.py` | that test plus `lint-imports` | 7 contracts kept | PASS |
| Clean suite 20/20 and leakage suite 20/20 | `tests/unit/test_rsi_plan.py` | `test_accepts_twenty_clean_fixtures_including_container_paths`, `test_rejects_twenty_leak_fixtures_covering_four_codes` | passed | PASS |
| `observed_p95_reexec_s` written, not invented | `scripts/measure_rsi_capacity.py`, `docs/ops/rsi-capacity.json` | capacity suite and `python3 scripts/measure_rsi_capacity.py` | null, go false | PASS |

**Full suite:** `PYTHONPATH=src python3 -m pytest tests/unit/test_rsi_bridge.py tests/unit/test_rsi_plan.py tests/unit/test_rsi_import_boundary.py tests/unit/test_rsi_capacity.py -q --noconftest -o addopts=` → 49 passed, 0 failed.

**Notes:**

- Phase A remains a no-go. `batch_size` is 0, `pool_headroom` is null, `slo_waiver` is null. The operator asked for Phase B anyway. Nothing in this change sets `RECERTIA_HYGIENE_ENABLED`.
- Ledger rows use action `write` and `evidence.kind` of `rsi_screened`, `rsi_negative`, or `rsi_idempotency`. `LedgerAction` is unchanged, so the schema does not move.
- A screened id is stored on `provenance.source_case_ids`. No field was added to `contracts.SkillVersion`.
- `observed_p95_reexec_s` is null because no opt-in batch was run. That is not a measured zero.
- Phase C and Phase D are not in this change.
