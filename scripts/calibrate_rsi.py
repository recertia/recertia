"""Offline calibration on a synthetic 30-row script. Not for activation.

Writes a candidate constant set and does not activate it.
"""

from __future__ import annotations

import json
from pathlib import Path

from recertia.rsi.models import CalibConstants
from recertia.rsi.scoring import accept_constants, score_run


def corpus(constants: CalibConstants):
    rows = []
    for i in range(20):
        rows.append(score_run(f"f{i}", "wide", 40, 40, False, constants))
    for i in range(10):
        rows.append(score_run(f"s{i}", "narrow", 10, 10, True, constants))
    return rows


def main() -> int:
    constants = CalibConstants(
        version="calib_candidate20261108",
        family_quantile={"wide": 40.0, "narrow": 10.0},
        approved_by="",
        approved_at="",
        partial_scale=0.6,
        fallback_d=82.0,
    )
    ok, detail = accept_constants(corpus(constants))
    out = {
        "activated": False,
        "accepted": ok,
        "detail": detail,
        "constants": {
            "version": constants.version,
            "family_quantile": constants.family_quantile,
            "partial_scale": constants.partial_scale,
            "fallback_d": constants.fallback_d,
        },
    }
    dest = Path(__file__).resolve().parents[1] / "candidate_constants.json"
    dest.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
