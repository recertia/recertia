"""Golden-corpus calibration. Not for activation.

Writes a candidate constant file from ``evals/golden/_promotion_logs``.
Does not call ``approve_gate`` or ``put_constants``. A green run is not a
T2 token. The synthetic script remains ``scripts/calibrate_rsi.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from recertia.ops.rsi_phase_c import golden_report  # noqa: E402


def main() -> int:
    report = golden_report(_ROOT)
    dest = _ROOT / "candidate_constants.json"
    dest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
