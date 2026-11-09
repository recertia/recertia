# RUNBOOK

From the repository root.

```bash
python3 -m pytest tests/unit/test_rsi_capacity.py -q --noconftest -o addopts=
python3 scripts/measure_rsi_capacity.py
```

The script prints `no-go` or `go` and rewrites `docs/ops/rsi-capacity.json`. It does not export `RECERTIA_HYGIENE_ENABLED`, `RECERTIA_CALIBRATED_SCORING`, or `RECERTIA_PHASE4_RETIREMENT`. It does not start a container.

The capacity file stays a no-go while `"go"` is false and `"slo_waiver"` is null. Do not export the hygiene flag from this script or from a worker. `observed_p95_reexec_s` stays null until a real opt-in batch is measured.
