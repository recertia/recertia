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

To open a soak on a real ledger, name the band yourself. The script does not
pick one and does not set a flag. `--started-at` needs a timezone. The baseline
promote is written one second earlier, marked `baseline: true`, and is the
restore row even if later promotions reuse the snapshot id.

```bash
PYTHONPATH=src python3 scripts/open_rsi_soak.py \
  --ledger /path/to/ledger.jsonl \
  --snapshot base \
  --post-change-skill sk_new \
  --noise-band <pre-registered-band> \
  --started-at 2026-11-09T00:00:00Z \
  --observations control-arm.json \
  --skills skills.json
```

`control-arm.json` is a list of existing soak observations. Only `arm: control`
rows count toward first-attempt success. No such rows leaves the rate null, and
the window cannot pass. `skills.json` is a list of `skill_id`, `legacy`, and
`prioritized`. Omit it to read `rsi_score_sidecar` rows that already name a
`skill_id`. An empty list exits 2 and does not write a soak result. The current
rate is not filled in from the baseline.
