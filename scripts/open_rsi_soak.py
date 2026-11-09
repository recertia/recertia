"""Open a Phase D soak on a production ledger.

Does not choose the noise band, does not invent a control-arm rate, and
does not set a flag. An empty skill list exits 2 without a soak row.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from recertia.ledger.hashchain import HashChainLedger  # noqa: E402
from recertia.ops.rsi_phase_c import DurableIndex  # noqa: E402
from recertia.ops.rsi_phase_d import SoakSkill, open_soak  # noqa: E402


def _started(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise SystemExit("started-at must include a timezone")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open a Phase D soak. Flags stay off.")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--post-change-skill", required=True)
    parser.add_argument("--noise-band", type=float, required=True)
    parser.add_argument("--started-at", required=True, help="ISO-8601 with a timezone")
    parser.add_argument("--observations", type=Path, help="JSON list of soak observations")
    parser.add_argument("--skills", type=Path, help="JSON list of skill_id, legacy, prioritized")
    args = parser.parse_args(argv)
    observations = []
    if args.observations is not None:
        observations = json.loads(args.observations.read_text(encoding="utf-8"))
        if not isinstance(observations, list):
            raise SystemExit("observations must be a JSON list")
    skills = None
    if args.skills is not None:
        raw = json.loads(args.skills.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit("skills must be a JSON list")
        skills = [
            SoakSkill(str(item["skill_id"]), float(item["legacy"]), float(item["prioritized"]))
            for item in raw
        ]
    ledger = HashChainLedger(args.ledger)
    result = open_soak(
        ledger,
        DurableIndex(ledger),
        started_at=_started(args.started_at),
        now=datetime.now(timezone.utc),
        baseline_snapshot_id=args.snapshot,
        post_change_skill_id=args.post_change_skill,
        noise_band=args.noise_band,
        skills=skills,
        observations=observations,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("status") == "not_opened":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
