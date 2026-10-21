"""REC-036: every job in ci.yml sets ``timeout-minutes``.

Without it GitHub's default is 360 minutes, so a hang in a job (for example a
test process that cannot exit) holds a runner for six hours. A line scan, not
a YAML parser: the dev extras do not ship PyYAML, and ci.yml keeps jobs at two
spaces and job keys at four.
"""

from __future__ import annotations

import re
from pathlib import Path

CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
MAX_MINUTES = 60
# Jobs whose timeout another open PR adds. Drop the entry once that PR is on main.
PENDING: dict[str, str] = {}


def _job_timeouts() -> dict[str, int | None]:
    jobs: dict[str, int | None] = {}
    in_jobs = False
    current: str | None = None
    for line in CI_YML.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\S", line):
            in_jobs = line.rstrip() == "jobs:"
            current = None
            continue
        if not in_jobs:
            continue
        job = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if job:
            current = job.group(1)
            jobs[current] = None
            continue
        timeout = re.match(r"^    timeout-minutes:\s*(\d+)\s*(#.*)?$", line)
        if timeout and current is not None:
            jobs[current] = int(timeout.group(1))
    return jobs


def test_ci_yml_has_jobs() -> None:
    assert {"ci", "commit-emails", "package-smoke", "container-smoke"} <= set(_job_timeouts())


def test_every_ci_job_has_a_bounded_timeout() -> None:
    timeouts = _job_timeouts()
    missing = sorted(name for name, minutes in timeouts.items() if minutes is None and name not in PENDING)
    assert missing == [], f"ci.yml jobs without timeout-minutes: {missing}"
    too_long = {name: m for name, m in timeouts.items() if m is not None and not 0 < m <= MAX_MINUTES}
    assert too_long == {}, f"timeout-minutes outside 1..{MAX_MINUTES}: {too_long}"
