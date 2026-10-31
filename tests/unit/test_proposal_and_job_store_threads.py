"""REC-114: threads that share a ``ProposalStore`` or ``JobRunStore``.

Both stores keep one SQLite connection that every request thread uses, like the program
store (REC-112). Unlocked reads on it could get no row, or a malformed one, for a record that
exists. Separately, ``ProposalStore.decide`` checked "pending" and then wrote, so two decisions
on one proposal at the same time could both succeed.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from recertia.api.jobs_store import JobRunStore
from recertia.proposals.store import ProposalRecord, ProposalStore
from tests.support.threads import run_together

NOT_PENDING = "is not pending"
WINNER = {"approve": "approved", "reject": "rejected"}


def _proposal(proposal_id: str, tenant_id: str = "t1") -> ProposalRecord:
    return ProposalRecord(
        proposal_id=proposal_id, kind="mine", skill_id="s", version=1, rationale="r", tenant_id=tenant_id
    )


class _Outcomes:
    def __init__(self) -> None:
        self.seen: Counter[str] = Counter()
        self._guard = threading.Lock()

    def note(self, outcome: str) -> None:
        with self._guard:
            self.seen[outcome] += 1


def test_threads_sharing_one_proposal_store_always_read_a_proposal_that_exists(tmp_path: Path) -> None:
    store = ProposalStore(tmp_path / "proposals.sqlite")
    try:
        store.add(_proposal("p0"))
        out = _Outcomes()

        def add(n: int) -> None:
            for i in range(50):
                try:
                    store.add(_proposal(f"w{n}-{i}", tenant_id="t2"))
                    out.note("added")
                except Exception as exc:  # any failure is the bug
                    out.note(f"add {type(exc).__name__}")

        def read() -> None:
            for _ in range(100):
                try:
                    got = store.get("p0", tenant_id="t1")
                    out.note("found" if got is not None and got.proposal_id == "p0" else "get wrong")
                    listed = [p.proposal_id for p in store.list(tenant_id="t1")]
                    out.note("listed" if listed == ["p0"] else f"list {len(listed)}")
                except Exception as exc:
                    out.note(f"read {type(exc).__name__}")

        def get_only() -> None:
            for _ in range(300):
                try:
                    got = store.get("p0", tenant_id="t1")
                    out.note("found" if got is not None and got.proposal_id == "p0" else "get wrong")
                except Exception as exc:
                    out.note(f"get {type(exc).__name__}")

        def list_only() -> None:
            for _ in range(200):
                try:
                    listed = [p.proposal_id for p in store.list(tenant_id="t1")]
                    out.note("listed" if listed == ["p0"] else f"list {len(listed)}")
                except Exception as exc:
                    out.note(f"list {type(exc).__name__}")

        run_together(
            [lambda n=n: add(n) for n in range(2)]
            + [read for _ in range(4)]
            + [get_only for _ in range(4)]
            + [list_only for _ in range(2)]
        )

        assert out.seen == {"added": 100, "found": 1600, "listed": 800}, out.seen
    finally:
        store.close()


def test_threads_sharing_one_job_run_store_always_read_a_run_that_exists(tmp_path: Path) -> None:
    store = JobRunStore(tmp_path / "jobs.sqlite")
    try:
        rec = store.create("mine", tenant_id="t1")
        out = _Outcomes()

        def save(n: int) -> None:
            for i in range(50):
                try:
                    store.save(replace(rec, status="running", meta={"w": f"{n}-{i}"}))
                    out.note("saved")
                except Exception as exc:
                    out.note(f"save {type(exc).__name__}")

        def read() -> None:
            for _ in range(100):
                try:
                    got = store.get(rec.job_run_id, tenant_id="t1")
                    out.note("found" if got is not None and got.job_run_id == rec.job_run_id else "get wrong")
                    listed = [r.job_run_id for r in store.list(tenant_id="t1")]
                    out.note("listed" if listed == [rec.job_run_id] else f"list {len(listed)}")
                except Exception as exc:
                    out.note(f"read {type(exc).__name__}")

        def get_only() -> None:
            for _ in range(300):
                try:
                    got = store.get(rec.job_run_id, tenant_id="t1")
                    out.note("found" if got is not None and got.job_run_id == rec.job_run_id else "get wrong")
                except Exception as exc:
                    out.note(f"get {type(exc).__name__}")

        def list_only() -> None:
            for _ in range(400):
                try:
                    listed = [r.job_run_id for r in store.list(tenant_id="t1")]
                    out.note("listed" if listed == [rec.job_run_id] else f"list {len(listed)}")
                except Exception as exc:
                    out.note(f"list {type(exc).__name__}")

        run_together(
            [lambda n=n: save(n) for n in range(2)]
            + [read for _ in range(4)]
            + [get_only for _ in range(4)]
            + [list_only for _ in range(2)]
        )

        assert out.seen == {"saved": 100, "found": 1600, "listed": 1200}, out.seen
    finally:
        store.close()


def _decide_race(stores: list[ProposalStore], trials: int) -> list[dict[str, str]]:
    """Per trial, approve on one thread and reject on another, at once, on a new proposal."""

    results = []
    for t in range(trials):
        stores[0].add(_proposal(f"d{t}"))
        got: dict[str, str] = {}

        def decide(
            decision: str, store: ProposalStore, pid: str = f"d{t}", got: dict[str, str] = got
        ) -> None:
            try:
                got[decision] = store.decide(pid, tenant_id="t1", decision=decision, actor=decision).status
            except ValueError as exc:
                got[decision] = "not pending" if NOT_PENDING in str(exc) else f"ValueError {exc}"
            except Exception as exc:  # any other failure is a bug too
                got[decision] = type(exc).__name__

        run_together([lambda: decide("approve", stores[0]), lambda: decide("reject", stores[-1])])
        stored = stores[0].get(f"d{t}", tenant_id="t1")
        got["stored"] = stored.status if stored is not None else "missing"
        results.append(got)
    return results


def _exactly_one_wins(results: list[dict[str, str]]) -> list[dict[str, str]]:
    bad = []
    for got in results:
        winners = [d for d in WINNER if got[d] == WINNER[d]]
        losers = [d for d in WINNER if got[d] == "not pending"]
        if not (len(winners) == 1 and len(losers) == 1 and got["stored"] == WINNER[winners[0]]):
            bad.append(got)
    return bad


def test_two_concurrent_decisions_on_one_proposal_exactly_one_wins(tmp_path: Path) -> None:
    store = ProposalStore(tmp_path / "proposals.sqlite")
    try:
        bad = _exactly_one_wins(_decide_race([store], trials=50))
        assert bad == [], f"{len(bad)}/50 trials: {bad[:3]}"
    finally:
        store.close()


def test_two_decisions_through_two_connections_exactly_one_wins(tmp_path: Path) -> None:
    # Two stores on one file, as two processes would have: the store lock cannot order them,
    # so the write itself must refuse a proposal that is no longer pending.
    stores = [ProposalStore(tmp_path / "proposals.sqlite") for _ in range(2)]
    try:
        bad = _exactly_one_wins(_decide_race(stores, trials=50))
        assert bad == [], f"{len(bad)}/50 trials: {bad[:3]}"
    finally:
        for s in stores:
            s.close()


def test_two_decisions_that_both_read_pending_first_exactly_one_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hold each decision after its "pending" read until the other one has read too, so both
    # always reach the write having seen "pending": only the guarded write can refuse one.
    # The hold works only while decide reads through ``self.get``; ``waited`` proves it did.
    stores = [ProposalStore(tmp_path / "proposals.sqlite") for _ in range(2)]
    both_read = threading.Barrier(2, timeout=10)
    waited = _Outcomes()
    for s in stores:

        def get_then_wait(proposal_id: str, *, tenant_id: str, read: Any = s.get) -> ProposalRecord | None:
            rec: ProposalRecord | None = read(proposal_id, tenant_id=tenant_id)
            if (
                rec is not None
                and rec.status == "pending"
                and threading.current_thread() is not threading.main_thread()
            ):
                both_read.wait()
                waited.note("both read pending")
            return rec

        monkeypatch.setattr(s, "get", get_then_wait)
    try:
        bad = _exactly_one_wins(_decide_race(stores, trials=20))
        assert bad == [], f"{len(bad)}/20 trials: {bad[:3]}"
        assert waited.seen == {"both read pending": 2 * 20}, waited.seen
    finally:
        for s in stores:
            s.close()


class _OneAtATime:
    """Stands in for a store's connection and records when two threads use it at once.

    A statement starting with ``hold_on`` holds the connection for up to ``hold`` seconds, or
    until another thread gets in, so a statement run outside the store lock is caught every
    time, not by chance.
    """

    def __init__(self, conn: sqlite3.Connection, hold_on: str, hold: float = 0.05) -> None:
        self._conn, self._hold_on, self._hold = conn, hold_on, hold
        self._guard = threading.Lock()
        self._inside = 0
        self._other_got_in = threading.Event()
        self.overlaps = 0
        self.held = 0

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._guard:
            self._inside += 1
            if self._inside > 1:
                self.overlaps += 1
                self._other_got_in.set()
        try:
            if sql.startswith(self._hold_on):
                self.held += 1
                self._other_got_in.wait(self._hold)
            return self._conn.execute(sql, params)
        finally:
            with self._guard:
                self._inside -= 1

    def __enter__(self) -> sqlite3.Connection:
        return self._conn.__enter__()

    def __exit__(self, *exc: Any) -> bool | None:
        return self._conn.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_decide_writes_while_no_other_thread_uses_the_connection(tmp_path: Path) -> None:
    # Each decision's UPDATE waits on the shared connection for the other decision to get in.
    # The store lock keeps it out, so the wait ends by timeout and nothing overlaps.
    store = ProposalStore(tmp_path / "proposals.sqlite")
    conn = _OneAtATime(store._conn, hold_on="UPDATE proposals")
    store._conn = conn  # type: ignore[assignment]
    try:
        bad = _exactly_one_wins(_decide_race([store], trials=10))
        assert bad == [], f"{len(bad)}/10 trials: {bad[:3]}"
        assert conn.held >= 10, conn.held
        assert conn.overlaps == 0, conn.overlaps
    finally:
        store._conn = conn._conn
        store.close()


def test_job_run_list_reads_while_no_other_thread_uses_the_connection(tmp_path: Path) -> None:
    # Each list holds the shared connection until a thread that keeps reading the run gets in.
    # The store lock keeps it out, so the hold ends by timeout and nothing overlaps.
    store = JobRunStore(tmp_path / "jobs.sqlite")
    rec = store.create("mine", tenant_id="t1")
    conn = _OneAtATime(store._conn, hold_on="SELECT payload FROM job_runs WHERE tenant_id")
    store._conn = conn  # type: ignore[assignment]
    listing_done = threading.Event()
    out = _Outcomes()

    def list_runs() -> None:
        try:
            for _ in range(10):
                out.note(
                    "listed"
                    if [r.job_run_id for r in store.list(tenant_id="t1")] == [rec.job_run_id]
                    else "list wrong"
                )
        finally:
            listing_done.set()

    def keep_getting() -> None:
        while not listing_done.is_set():
            try:
                got = store.get(rec.job_run_id, tenant_id="t1")
                out.note("found" if got is not None else "get missing")
            except Exception as exc:  # an overlap can break the read; the counts below report it
                out.note(f"get {type(exc).__name__}")

    try:
        run_together([list_runs, keep_getting])
        assert conn.held == 10, conn.held
        assert conn.overlaps == 0, (conn.overlaps, out.seen)
        assert out.seen["listed"] == 10 and set(out.seen) <= {"listed", "found"}, out.seen
    finally:
        store._conn = conn._conn
        store.close()


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from recertia.ledger import HashChainLedger  # noqa: E402
from tests.support.idempotency import close_app, headers_for, make_app  # noqa: E402


def test_concurrent_approve_and_reject_through_the_api_one_wins_and_one_ledger_entry(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        headers = headers_for(app)
        proposals = app.state.recertia.proposals
        trials = 40
        statuses: list[list[int]] = []
        for t in range(trials):
            proposals.add(_proposal(f"api{t}"))
            got: dict[str, Any] = {}

            def post(decision: str, pid: str = f"api{t}", got: dict[str, Any] = got) -> None:
                got[decision] = client.post(
                    f"/v1/proposals/{pid}/decision", headers=headers, json={"decision": decision}
                )

            run_together([lambda: post("approve"), lambda: post("reject")])
            statuses.append(sorted(r.status_code for r in got.values()))
            loser = next((r for r in got.values() if r.status_code != 200), None)
            assert loser is None or NOT_PENDING in loser.text, loser.text

        # The loser answers the existing 400 "not pending", and only the winner is in the ledger.
        assert statuses == [[200, 400]] * trials
        ledger = HashChainLedger(tmp_path / "api-root" / "runs" / "t1" / "ledger.jsonl")
        decisions = [e for e in ledger.entries() if e.evidence.get("kind") == "proposal_decision"]
        assert len(decisions) == trials
    finally:
        close_app(app)


def _delete_proposal(path: Path, proposal_id: str) -> None:
    # As another process would: through its own connection on the same file.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM proposals WHERE proposal_id = ?", (proposal_id,))


@pytest.mark.parametrize(
    ("route", "not_found"),
    [("/v1/proposals/{}/decision", "proposal not found"), ("/v1/reviews/{}", "review not found")],
    ids=["proposal-decision", "review"],
)
@pytest.mark.parametrize(
    "vanish_after_read", [1, 2], ids=["after-the-library-read", "between-decide-read-and-update"]
)
def test_a_proposal_that_vanishes_during_a_decision_answers_404_and_no_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, not_found: str, vanish_after_read: int
) -> None:
    # Read 1 is the library's own lookup and read 2 is decide's "pending" read. Deleting the row
    # right after read 1 makes decide's read find no row; right after read 2, its UPDATE changes
    # no row and its re-read finds none. Either way the proposal is gone: 404, not 500.
    app = make_app(tmp_path)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        headers = headers_for(app)
        proposals = app.state.recertia.proposals
        proposals.add(_proposal("gone"))
        reads: list[str] = []

        def get_then_vanish(
            proposal_id: str, *, tenant_id: str, read: Any = proposals.get
        ) -> ProposalRecord | None:
            rec: ProposalRecord | None = read(proposal_id, tenant_id=tenant_id)
            reads.append("found" if rec is not None else "missing")
            if len(reads) == vanish_after_read:
                _delete_proposal(proposals.path, proposal_id)
            return rec

        monkeypatch.setattr(proposals, "get", get_then_vanish)
        resp = client.post(route.format("gone"), headers=headers, json={"decision": "approve"})

        assert resp.status_code == 404, resp.text
        assert not_found in resp.text, resp.text
        assert reads == ["found"] * vanish_after_read + ["missing"], reads
        ledger = HashChainLedger(tmp_path / "api-root" / "runs" / "t1" / "ledger.jsonl")
        decisions = {"proposal_decision", "review_decision"}
        assert [e for e in ledger.entries() if e.evidence.get("kind") in decisions] == []
    finally:
        close_app(app)
