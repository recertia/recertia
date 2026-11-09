# RUNBOOK

From the repository root. This phase does not export a flag.

```bash
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_rsi_phase_c.py \
  tests/unit/test_rsi_plan.py \
  tests/unit/test_rsi_import_boundary.py \
  tests/unit/test_rsi_capacity.py \
  -q --noconftest -o addopts=
python3 scripts/calibrate_rsi_golden.py
```

`scripts/calibrate_rsi.py` is the synthetic 30-row script. It is not for activation.
`scripts/calibrate_rsi_golden.py` reads `evals/golden/_promotion_logs` and writes
`candidate_constants.json` with `"activated": false`. It does not call `approve_gate`.

Do not set `RECERTIA_HYGIENE_ENABLED`, `RECERTIA_CALIBRATED_SCORING`, or
`RECERTIA_PHASE4_RETIREMENT`. Retirement still uses the legacy score.
`docs/ops/rsi-capacity.json` is still a no-go. That file is not a waiver.
