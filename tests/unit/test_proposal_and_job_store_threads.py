"""REC-114: threads that share a ``ProposalStore`` or ``JobRunStore``.

Both stores keep one SQLite connection that every request thread uses, like the program
store (REC-112). Unlocked reads on it could get no row, or a malformed one, for a record that
exists. Separately, ``ProposalStore.decide`` checked "pending" and then wrote, so two decisions
on one proposal at the same time could both succeed.
"""

from __future__ import annotations

import threading
from collections import Counter
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
            for _ in range(200):
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

        assert out.seen == {"saved": 100, "found": 1600, "listed": 800}, out.seen
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
