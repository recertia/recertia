# RUNBOOK

From the repository root. The hygiene flag is off unless the process sets it.

```bash
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_rsi_bridge.py \
  tests/unit/test_rsi_plan.py \
  tests/unit/test_rsi_import_boundary.py \
  tests/unit/test_rsi_capacity.py \
  -q --noconftest -o addopts=
python3 scripts/measure_rsi_capacity.py
```

The capacity script prints `no-go` or `go` and rewrites `docs/ops/rsi-capacity.json`. It does not export `RECERTIA_HYGIENE_ENABLED`, `RECERTIA_CALIBRATED_SCORING`, or `RECERTIA_PHASE4_RETIREMENT`. It does not start a container.

Do not set `RECERTIA_HYGIENE_ENABLED` in a worker while `docs/ops/rsi-capacity.json` has `"go": false` and `"slo_waiver": null`. With the variable unset, distill does not call the bridge. With it set to `1`, a verifier failure or a leakage finding does not write a skill candidate.
