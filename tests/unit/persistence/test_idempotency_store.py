"""REC-056e (#135): the control.db idempotency key store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from recertia.persistence import idempotency
from recertia.persistence.idempotency import (
    LEASE_SECONDS,
    Completion,
    IdempotencyStore,
    KeyScope,
    Outcome,
)
from recertia.runtime.errors import ConfigError

SCOPE = KeyScope("t1", "p1", "s1", "k1")
ENV = "RECERTIA_IDEMPOTENCY_TTL_SECONDS"


class _Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(tmp_path: Path, clock: _Clock) -> Any:
    built = IdempotencyStore(tmp_path / "control.db", clock=clock)
    yield built
    built.close()


def _rows(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return list(
            conn.execute(
                "SELECT tenant_id, program_id, step_id, idempotency_key, request_hash, state, run_id,"
                " created_at, expires_at FROM idempotency_keys ORDER BY rowid"
            )
        )
    finally:
        conn.close()


class _FailingConn:
    """Delegates to a real connection; raises ``error`` for statements starting with ``prefix``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.prefix: str | None = None
        self.error: Exception = sqlite3.OperationalError("disk I/O error")

    def execute(self, sql: str, *args: Any) -> Any:
        if self.prefix is not None and sql.lstrip().startswith(self.prefix):
            raise self.error
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@pytest.fixture
def failing(store: Any, monkeypatch: pytest.MonkeyPatch) -> _FailingConn:
    real_open = idempotency.open_db
    holder: list[_FailingConn] = []

    def open_failing(path: Any, **kwargs: Any) -> Any:
        holder.append(_FailingConn(real_open(path, **kwargs)))
        return holder[-1]

    monkeypatch.setattr(idempotency, "open_db", open_failing)
    store.reserve(KeyScope("t0", "p0", "s0", "warm"), "v1:warm")  # opens and migrates
    return holder[0]


# --- TTL setting --------------------------------------------------------------------------


def test_ttl_defaults_to_a_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert IdempotencyStore(tmp_path / "control.db").ttl_seconds == 86400


@pytest.mark.parametrize("raw", ["60", "2592000", " 3600 "])
def test_ttl_in_range_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV, raw)
    assert IdempotencyStore(tmp_path / "control.db").ttl_seconds == int(raw)


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "59", "2592001", "1.5"])
def test_a_bad_ttl_fails_store_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(ENV, raw)
    with pytest.raises(ConfigError, match=ENV):
        IdempotencyStore(tmp_path / "control.db")


# --- lifecycle ----------------------------------------------------------------------------


def test_building_and_closing_the_store_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "control.db"
    built = IdempotencyStore(path)
    built.close()
    built.close()
    assert built.closed
    assert not path.parent.exists()


def test_a_closed_store_refuses_writes(store: Any) -> None:
    store.reserve(SCOPE, "v1:a")
    store.close()
    assert store.closed and store._conn is None
    with pytest.raises(RuntimeError, match="closed"):
        store.reserve(SCOPE, "v1:a")


def test_the_first_write_applies_control_migrations(store: Any, tmp_path: Path) -> None:
    store.reserve(SCOPE, "v1:a")
    conn = sqlite3.connect(tmp_path / "control.db")
    try:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()
    assert versions[-1] == "003_idempotency"


# --- reserve, complete, release -----------------------------------------------------------


def test_reserve_then_complete_then_replay(store: Any, tmp_path: Path, clock: _Clock) -> None:
    held = store.reserve(SCOPE, "v1:a")
    assert (held.outcome, held.lease) == (Outcome.RESERVED, clock.now)
    now, exp = clock.now, clock.now + store.ttl_seconds
    assert _rows(tmp_path / "control.db") == [("t1", "p1", "s1", "k1", "v1:a", "pending", None, now, exp)]
    assert store.complete(SCOPE, "v1:a", "run-1", lease=now) is Completion.COMPLETED
    assert _rows(tmp_path / "control.db") == [("t1", "p1", "s1", "k1", "v1:a", "complete", "run-1", now, exp)]
    replay = store.reserve(SCOPE, "v1:a")
    assert (replay.outcome, replay.run_id) == (Outcome.REPLAY, "run-1")


def test_a_different_hash_is_a_mismatch_and_changes_nothing(store: Any, tmp_path: Path) -> None:
    lease = store.reserve(SCOPE, "v1:a").lease
    pending = _rows(tmp_path / "control.db")
    assert store.reserve(SCOPE, "v1:b").outcome is Outcome.MISMATCH
    store.complete(SCOPE, "v1:a", "run-1", lease=lease)
    complete = _rows(tmp_path / "control.db")
    assert store.reserve(SCOPE, "v1:b").outcome is Outcome.MISMATCH
    assert store.complete(SCOPE, "v1:b", "run-2", lease=lease) is Completion.LOST
    store.release(SCOPE, "v1:b", lease=lease)
    assert pending[0][5] == "pending" and _rows(tmp_path / "control.db") == complete


def test_a_pending_row_is_in_progress_inside_its_lease_then_taken_over_once(
    store: Any, tmp_path: Path, clock: _Clock
) -> None:
    start = clock.now
    assert store.reserve(SCOPE, "v1:a").outcome is Outcome.RESERVED
    clock.now = start + LEASE_SECONDS - 1
    assert store.reserve(SCOPE, "v1:a").outcome is Outcome.IN_PROGRESS
    clock.now = start + LEASE_SECONDS
    taken = store.reserve(SCOPE, "v1:a")
    assert (taken.outcome, taken.lease) == (Outcome.TAKEN_OVER, clock.now)
    assert _rows(tmp_path / "control.db")[0][5:8] == ("pending", None, clock.now)
    clock.now += 1
    assert store.reserve(SCOPE, "v1:a").outcome is Outcome.IN_PROGRESS
    assert store.complete(SCOPE, "v1:a", "run-1", lease=taken.lease) is Completion.COMPLETED


def test_release_deletes_only_this_requests_pending_row(store: Any, tmp_path: Path) -> None:
    other = KeyScope("t1", "p1", "s1", "k2")
    lease = store.reserve(SCOPE, "v1:a").lease
    other_lease = store.reserve(other, "v1:a").lease
    store.complete(other, "v1:a", "run-1", lease=other_lease)
    store.release(SCOPE, "v1:a", lease=lease)
    store.release(other, "v1:a", lease=other_lease)  # complete rows are never released
    assert [row[3:7] for row in _rows(tmp_path / "control.db")] == [("k2", "v1:a", "complete", "run-1")]
    assert store.complete(SCOPE, "v1:a", "run-1", lease=lease) is Completion.LOST


@pytest.mark.parametrize(
    "scopes",
    [
        [KeyScope("t1", "p", "s", "k"), KeyScope("t2", "p", "s", "k")],
        [KeyScope("t1", "p:1", "s", "k"), KeyScope("t1", "p", "1:s", "k")],
        [KeyScope("t1:p", "s", "x", "k"), KeyScope("t1", "p:s", "x", "k")],
        [KeyScope("t1", "p", "s", "a:b"), KeyScope("t1", "p", "s:a", "b")],
    ],
)
def test_scopes_that_join_to_the_same_string_do_not_collide(store: Any, scopes: list[KeyScope]) -> None:
    outcomes = [store.reserve(scope, f"v1:{n}").outcome for n, scope in enumerate(scopes)]
    assert outcomes == [Outcome.RESERVED] * 2


# --- expiry and sweep ---------------------------------------------------------------------


def test_an_expired_row_is_not_honoured_even_when_the_sweep_skips_it(
    store: Any, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = store.reserve(SCOPE, "v1:a").lease
    store.complete(SCOPE, "v1:a", "run-1", lease=lease)
    monkeypatch.setattr(idempotency, "SWEEP_LIMIT", 0)
    clock.now += store.ttl_seconds - 1
    assert store.reserve(SCOPE, "v1:b").outcome is Outcome.MISMATCH
    clock.now += 1
    assert store.reserve(SCOPE, "v1:b").outcome is Outcome.RESERVED


def test_a_write_sweeps_at_most_500_expired_rows(store: Any, tmp_path: Path, clock: _Clock) -> None:
    store.reserve(SCOPE, "v1:warm")
    conn = sqlite3.connect(tmp_path / "control.db")
    try:
        conn.executemany(
            "INSERT INTO idempotency_keys VALUES ('t9', 'p', 's', ?, 'v1:x', 'complete', 'r', 0, ?)",
            [(f"old-{n:04d}", clock.now - n) for n in range(502)],
        )
        conn.commit()
    finally:
        conn.close()
    fresh = store.reserve(KeyScope("t1", "p1", "s1", "fresh"), "v1:a")
    left = [row for row in _rows(tmp_path / "control.db") if row[0] == "t9"]
    assert len(left) == 2
    store.release(KeyScope("t1", "p1", "s1", "fresh"), "v1:a", lease=fresh.lease)  # a release does not sweep
    assert len([row for row in _rows(tmp_path / "control.db") if row[0] == "t9"]) == 2
    store.reserve(KeyScope("t1", "p1", "s1", "next"), "v1:a")
    assert [row for row in _rows(tmp_path / "control.db") if row[0] == "t9"] == []


# --- errors and transactions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "scope"),
    [
        ("DELETE FROM idempotency_keys WHERE rowid", SCOPE),  # the sweep
        ("INSERT", KeyScope("t1", "p1", "s1", "k-new")),
        ("SELECT request_hash", SCOPE),  # reading the conflicting row
    ],
)
def test_a_sqlite_error_in_reserve_propagates_and_writes_nothing(
    store: Any, failing: _FailingConn, tmp_path: Path, prefix: str, scope: KeyScope
) -> None:
    store.reserve(SCOPE, "v1:a")
    before = _rows(tmp_path / "control.db")
    failing.prefix = prefix
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        store.reserve(scope, "v1:b")
    assert _rows(tmp_path / "control.db") == before
    assert not store._conn.in_transaction


@pytest.mark.parametrize("call", ["complete", "release"])
def test_a_sqlite_error_in_complete_or_release_propagates(
    store: Any, failing: _FailingConn, tmp_path: Path, call: str
) -> None:
    lease = store.reserve(SCOPE, "v1:a").lease
    failing.prefix = "UPDATE" if call == "complete" else "DELETE"
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        if call == "complete":
            store.complete(SCOPE, "v1:a", "run-1", lease=lease)
        else:
            store.release(SCOPE, "v1:a", lease=lease)
    assert [row[5] for row in _rows(tmp_path / "control.db") if row[3] == "k1"] == ["pending"]


def test_an_integrity_error_other_than_the_primary_key_propagates(store: Any) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.reserve(SCOPE, None)


def test_every_write_is_one_begin_immediate_transaction(store: Any) -> None:
    store.reserve(KeyScope("t0", "p0", "s0", "warm"), "v1:warm")
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    lease = store.reserve(SCOPE, "v1:a").lease
    store.reserve(SCOPE, "v1:b")
    store.complete(SCOPE, "v1:a", "run-1", lease=lease)
    store.release(SCOPE, "v1:a", lease=lease)
    begins = [s.strip().upper() for s in statements if s.strip().upper().startswith("BEGIN")]
    ends = [s.strip().upper() for s in statements if s.strip().upper() in {"COMMIT", "ROLLBACK"}]
    assert begins == ["BEGIN IMMEDIATE"] * 4
    assert ends == ["COMMIT"] * 4


# --- fencing (review S1, N3) and COMMIT failure (N2) --------------------------------------


def test_a_stale_holder_can_neither_release_nor_complete_a_taken_over_row(
    store: Any, tmp_path: Path, clock: _Clock
) -> None:
    slow = store.reserve(SCOPE, "v1:a")
    clock.now += LEASE_SECONDS
    taker = store.reserve(SCOPE, "v1:a")
    assert taker.outcome is Outcome.TAKEN_OVER and taker.lease != slow.lease
    taken = _rows(tmp_path / "control.db")

    store.release(SCOPE, "v1:a", lease=slow.lease)
    assert _rows(tmp_path / "control.db") == taken
    assert store.reserve(SCOPE, "v1:b").outcome is Outcome.MISMATCH
    assert store.complete(SCOPE, "v1:a", "run-1", lease=slow.lease) is Completion.SUPERSEDED
    assert _rows(tmp_path / "control.db") == taken

    assert store.complete(SCOPE, "v1:a", "run-1", lease=taker.lease) is Completion.COMPLETED


def test_completing_a_row_that_already_records_this_run_id_succeeds(store: Any, clock: _Clock) -> None:
    slow = store.reserve(SCOPE, "v1:a")
    clock.now += LEASE_SECONDS
    taker = store.reserve(SCOPE, "v1:a")
    assert store.complete(SCOPE, "v1:a", "run-1", lease=taker.lease) is Completion.COMPLETED

    assert store.complete(SCOPE, "v1:a", "run-1", lease=slow.lease) is Completion.COMPLETED
    assert store.complete(SCOPE, "v1:a", "run-2", lease=slow.lease) is Completion.LOST
    assert store.complete(SCOPE, "v1:b", "run-1", lease=slow.lease) is Completion.LOST


def test_complete_matches_the_request_hash(store: Any, tmp_path: Path) -> None:
    held = store.reserve(SCOPE, "v1:a")
    before = _rows(tmp_path / "control.db")
    assert store.complete(SCOPE, "v1:b", "run-1", lease=held.lease) is Completion.LOST
    assert _rows(tmp_path / "control.db") == before
    assert store.complete(SCOPE, "v1:a", "run-1", lease=held.lease) is Completion.COMPLETED


def test_a_failed_commit_rolls_back_and_the_writer_recovers(
    store: Any, failing: _FailingConn, tmp_path: Path
) -> None:
    before = _rows(tmp_path / "control.db")
    failing.prefix = "COMMIT"
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        store.reserve(SCOPE, "v1:a")
    assert not store._conn.in_transaction
    assert _rows(tmp_path / "control.db") == before
    failing.prefix = None
    assert store.reserve(SCOPE, "v1:a").outcome is Outcome.RESERVED
