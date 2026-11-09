"""Write the Phase D soak result for this checkout.

The file records an open window. It is not a pass, not an abort, and not
permission to change a flag default. Default-on is not scheduled.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from recertia.ops.rsi_phase_d import checkout_record  # noqa: E402


def main() -> int:
    record = checkout_record(datetime.now(timezone.utc))
    dest = _ROOT / "docs" / "ops" / "rsi-soak.json"
    dest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
