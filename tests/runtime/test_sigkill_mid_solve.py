"""Real SIGKILL of the orchestrator process while ``solve`` is running a command.

max_steps-based tests stop cleanly at a node boundary; this kills the OS process mid-node
(SQLite checkpoint/op-ledger and ledger.jsonl possibly mid-write) and resumes in a new process.
POSIX only.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from recertia.graph.engine import GraphOrchestrator

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

REPO = Path(__file__).resolve().parents[2]

DRIVER = textwrap.dedent(
    """
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    from contracts.criteria import TaskCriterion, mint_rejecting_proof
    from contracts.run import Task
    from recertia.graph.engine import GraphOrchestrator

    runs, wd = Path(sys.argv[1]), Path(sys.argv[2])
    base = TaskCriterion(id="output-exists", kind="command", run="test -f output.txt",
                         source="caller", weight=1.0)
    crit = base.model_copy(update={"sensitivity_proof": mint_rejecting_proof(
        base, negative_fixture="empty workspace", fingerprint="sigkill")})
    script = [
        "python3 -c \\"open('started.flag','w').write('1')\\"",
        "sleep 30",
        "python3 -c \\"open('output.txt','w').write('done')\\"",
    ]
    task = Task(task_id="t1", request="w", submitted_at=datetime.now(timezone.utc))
    GraphOrchestrator(runs).start("run-kill", task, [crit], workdir=wd, script=script)
    """
)

RESUME_SCRIPT = [
    "python3 -c \"open('started.flag','w').write('1')\"",
    "true",
    "python3 -c \"open('output.txt','w').write('done')\"",
]


def test_sigkill_during_solve_then_resume(tmp_path: Path) -> None:
    runs, wd = tmp_path / "runs", tmp_path / "ws"
    wd.mkdir()
    env = {**os.environ, "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}"}
    proc = subprocess.Popen(
        [sys.executable, "-c", DRIVER, str(runs), str(wd)],
        env=env,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while not (wd / "started.flag").exists():
        assert proc.poll() is None, "driver exited before solve started"
        assert time.monotonic() < deadline, "solve never started"
        time.sleep(0.05)
    time.sleep(0.3)
    os.killpg(proc.pid, signal.SIGKILL)  # kill driver and its `sleep 30` child
    proc.wait(timeout=10)

    orch = GraphOrchestrator(runs)
    try:
        orch.ledger.verify()  # tape must still verify after a hard kill
        t0 = time.monotonic()
        state = orch.resume("run-kill", workdir=wd, script=RESUME_SCRIPT)
        assert time.monotonic() - t0 < 60
    finally:
        orch.close()
    assert state.terminal == "solved"
