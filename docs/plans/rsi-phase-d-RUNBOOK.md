# RUNBOOK

From the repository root. This phase does not export a flag and does not schedule default-on.

```bash
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_rsi_phase_d.py \
  tests/unit/test_rsi_phase_c.py \
  tests/unit/test_rsi_plan.py \
  tests/unit/test_rsi_import_boundary.py \
  -q --noconftest -o addopts=
python3 scripts/record_rsi_soak.py
```

`docs/ops/rsi-soak.json` is the written result for this checkout. It is open.
It is not a pass. Do not read it as permission to set `RECERTIA_HYGIENE_ENABLED`
or `RECERTIA_CALIBRATED_SCORING`.

A real soak passes only when all of these are true:

- 14 days have elapsed since the soak start, and
- 30 promotion decisions have been recorded on the ledger inside that window, and
- no skill the legacy score would keep would be dropped by the prioritized score
  at the current retirement threshold, and
- control-arm first-attempt success, from the existing soak, is inside the
  pre-registered noise band.

Abort if either comparison fails. Abort calls the Phase C rollback: flags forced
off, index pointer restored to the baseline promote, receipts kept, one incident
appended. The finding is not a switch to the new score.

`docs/ops/rsi-capacity.json` is still a no-go. That file is not a waiver.
