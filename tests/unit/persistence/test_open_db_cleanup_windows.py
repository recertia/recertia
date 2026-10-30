"""REC-056-nits C8: ``open_db`` closes the connection it opened when setup fails.

``windows``-marked: on Windows an open handle keeps the file from being removed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence import db as db_mod

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("profile", ["durable", "bulk", "ephemeral"])
def test_open_db_closes_the_connection_when_setup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: Any
) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    def failing_journal_mode(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("injected journal mode failure")

    monkeypatch.setattr(db_mod.sqlite3, "connect", recording_connect)
    monkeypatch.setattr(db_mod, "_set_journal_mode", failing_journal_mode)

    with pytest.raises(sqlite3.OperationalError, match="^injected journal mode failure$"):
        db_mod.open_db(tmp_path / "x.db", profile=profile)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    (tmp_path / "x.db").unlink(missing_ok=True)
