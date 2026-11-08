# RUNBOOK

From `the repository root`:

```bash
python -m pytest tests/unit/test_rsi_plan.py tests/unit/test_rsi_import_boundary.py -q
python scripts/calibrate_rsi.py
```

Quiet suite:

```bash
python -m pytest tests/unit/test_rsi_plan.py tests/unit/test_rsi_import_boundary.py -q
```

Calibration writes `candidate_constants.json` with `"activated": false`. It does not call `install_constants`.

Flags stay off unless the process environment sets `RECERTIA_HYGIENE_ENABLED=1` or `RECERTIA_CALIBRATED_SCORING=1`. Do not set those for the default suite.

Rollback behavior covered by `PipelineSuite.test_rollback_restores_baseline_generation`: flags forced off, live index pointer restored to the baseline promote generation, receipts kept.
