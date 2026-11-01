from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.run import RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.ledger import HashChainLedger
from recertia.nodes.context import NodeContext
from recertia.workspace import WorkspaceManager


@pytest.fixture
def base_state() -> RunState:
    return RunState(
        run_id="unit-run",
        task=Task(task_id="t", request="do a thing", submitted_at=datetime.now(timezone.utc)),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[NodeContext]:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    ops = OperationLedger(tmp_path / "ops.db")
    try:
        yield NodeContext(
            run_id="unit-run",
            attempt_no=0,
            node="test",
            workdir=workdir,
            workspaces=WorkspaceManager(tmp_path / "snapshots"),
            ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
            ops=ops,
            script=None,
        )
    finally:
        ops.close()
