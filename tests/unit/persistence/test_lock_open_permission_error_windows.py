"""Real-Windows PermissionError from the root-lock open ends the async run (#256).

On Windows, ``os.open`` of a path that is already a directory raises
``PermissionError`` (EACCES). That is a real non-busy open failure: no
monkeypatch of ``open`` or the lock code. The ``windows`` marker selects this
module on the ``root lock (windows-latest)`` job's windows-marked step; the
test name deliberately omits the substring ``root_lock`` so the root-lock
``-k`` selection count stays unchanged.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from recertia.persistence.layout import RuntimeLayout
from tests.unit.workers.test_run_worker_open_failure import (
    _assert_terminal_and_path_free,
    _open_failure_records,
    _run_to_failure,
)

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="real Windows only"),
]


def test_lock_path_as_directory_ends_the_run_with_permission_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-create the lock path as a directory so the open raises PermissionError."""

    root = tmp_path / "host-only-dir"
    lock_path = RuntimeLayout(root).root_lock
    lock_path.mkdir(parents=True)
    with caplog.at_level(logging.ERROR, logger="recertia.workers.run_worker"):
        outcome, log, failed = _run_to_failure(root)
    _assert_terminal_and_path_free(root, outcome, log, failed)
    records = _open_failure_records(caplog)
    assert len(records) == 1, caplog.records
    record = records[0]
    assert record.exc_info is not None and isinstance(record.exc_info[1], PermissionError), (
        record.exc_info
    )
