"""``GET /v1/runs/{run_id}/events?follow=1``: byte-offset SSE ids, resume, caps, termination."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from recertia.api import create_app, event_follow
from recertia.api.event_follow import EventFollower, FollowLimiter, FollowSettings, InvalidOffset
from recertia.api.routes.follow_stream import SSE_HEADERS, aiter_follow
from recertia.errors import ConfigError
from recertia.workers import queue as run_queue
from recertia.workers.queue import QueueItem

KNOBS = {
    "RECERTIA_SSE_POLL_S": "0.02",
    "RECERTIA_SSE_MAX_S": "3",
    "RECERTIA_SSE_KEEPALIVE_S": "15",
}


@dataclass
class Env:
    app: Any
    events: Any
    client: TestClient
    root: Path
    key1: str
    key2: str

    def log_path(self, run: str = "r", tenant: str = "t1") -> Path:
        return self.root / "runs" / tenant / "run_events" / f"{run}.jsonl"

    def get(
        self,
        run: str = "r",
        *,
        key: str | None = None,
        leid: str | None = None,
        query: str = "follow=1",
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> Any:
        hdrs: list[tuple[str, str]] = [("X-API-Key", key or self.key1)]
        if leid is not None:
            hdrs.append(("Last-Event-ID", leid))
        if isinstance(headers, dict):
            hdrs.extend(headers.items())
        elif headers:
            hdrs.extend(headers)
        return self.client.get(f"/v1/runs/{run}/events?{query}", headers=hdrs)

    def seed(self, run: str = "r", n: int = 3, *, finish: bool = True, tenant: str = "t1") -> Path:
        for i in range(n):
            self.events.append(tenant, run, "tick", {"i": i})
        if finish:
            self.events.append(tenant, run, "run.finished", {"terminal": "solved"})
        return self.log_path(run, tenant)


@pytest.fixture()
def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Env]:
    def build(**knobs: str) -> Env:
        for name, value in {**KNOBS, **knobs}.items():
            monkeypatch.setenv(name, value)
        root = tmp_path / "root"
        app = create_app(root=root)
        k1 = app.state.api_keys.issue(tenant_id="t1", scopes={"runs"}, actor="test").secret
        k2 = app.state.api_keys.issue(tenant_id="t2", scopes={"runs"}, actor="test").secret
        return Env(
            app, app.state.recertia.events, TestClient(app, raise_server_exceptions=False), root, k1, k2
        )

    return build


@pytest.fixture()
def env(make_env: Callable[..., Env]) -> Env:
    return make_env()


def parse(text: str) -> list[dict[str, str]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        out.append(dict(line.split(": ", 1) for line in block.split("\n")))
    return out


def types(text: str) -> list[str]:
    return [json.loads(f["data"])["type"] for f in parse(text)]


def ids(text: str) -> list[int]:
    return [int(f["id"]) for f in parse(text)]


def line_ends(path: Path) -> list[int]:
    ends, acc = [], 0
    for line in path.read_bytes().split(b"\n")[:-1]:
        acc += len(line) + 1
        ends.append(acc)
    return ends


def raw_line(event_id: str, event_type: str, payload: dict[str, Any] | None = None) -> bytes:
    event = {"event_id": event_id, "type": event_type, "payload": payload or {}}
    return json.dumps(event, separators=(",", ":")).encode() + b"\n"


# --- (a) streams events appended after connect --------------------------------------------


def test_follow_emits_events_appended_after_connect(env: Env) -> None:
    env.events.append("t1", "r", "run.queued", {})
    it = env.events.follow("t1", "r", poll_s=0.02, max_seconds=5)
    assert json.loads(parse(next(it))[0]["data"])["type"] == "run.queued"

    def writer() -> None:
        time.sleep(0.2)
        env.events.append("t1", "r", "tick", {"n": 1})
        env.events.append("t1", "r", "run.finished", {"terminal": "solved"})

    thread = threading.Thread(target=writer)
    thread.start()
    rest = [f for f in it if not f.startswith(":")]
    thread.join()
    assert [json.loads(parse(f)[0]["data"])["type"] for f in rest] == ["tick", "run.finished"]

    last_id = int(parse(rest[-1])[0]["id"])
    assert last_id == env.log_path().stat().st_size
    assert list(env.events.follow("t1", "r", after_offset=last_id, max_seconds=1, poll_s=0.02)) == []


def test_route_streams_events_appended_after_connect(env: Env) -> None:
    env.events.append("t1", "r", "run.queued", {})

    def writer() -> None:
        time.sleep(0.4)
        env.events.append("t1", "r", "tick", {"n": 1})
        env.events.append("t1", "r", "run.finished", {"terminal": "solved"})

    thread = threading.Thread(target=writer)
    thread.start()
    started = time.monotonic()
    resp = env.get()
    elapsed = time.monotonic() - started
    thread.join()
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert types(resp.text) == ["run.queued", "tick", "run.finished"]
    assert elapsed >= 0.3  # the response stayed open until the appended events arrived
    assert ids(resp.text) == line_ends(env.log_path())


# --- (b) reconnect replays nothing already seen --------------------------------------------


def test_reconnect_with_last_event_id_replays_nothing_seen(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.4")
    path = env.seed(n=3, finish=False)
    first = env.get()
    assert types(first.text) == ["tick"] * 3
    seen = ids(first.text)

    resumed = env.get(leid=str(seen[0]))
    assert ids(resumed.text) == seen[1:]

    assert parse(env.get(leid=str(seen[-1])).text) == []

    env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
    tail = env.get(leid=str(seen[-1]))
    assert types(tail.text) == ["run.finished"]
    assert ids(tail.text) == [path.stat().st_size]


def test_resume_from_a_terminal_run_returns_at_once(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    path = env.seed()
    started = time.monotonic()
    resp = env.get(leid=str(path.stat().st_size))
    assert resp.status_code == 200
    assert parse(resp.text) == []
    assert time.monotonic() - started < 2


# --- every frame carries its own end offset ------------------------------------------


def test_each_frame_id_is_the_offset_after_its_own_event(env: Env) -> None:
    path = env.seed(n=3)
    ends = line_ends(path)
    assert len(ends) == 4
    assert ids(env.get().text) == ends


def test_resume_after_any_frame_loses_nothing(env: Env) -> None:
    path = env.seed(n=3)
    ends = line_ends(path)
    full = types(env.get().text)
    for i, end in enumerate(ends):
        assert types(env.get(leid=str(end)).text) == full[i + 1 :]


# --- (c) a bad cursor is a real 400 before any streaming -----------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "abc",
        "+5",
        " 5 ",
        "\t5",
        "1_0",
        "-1",
        "-0",
        "1.5",
        "1e3",
        "0x10",
        "5, 7",
        "",
        "1234567890123456",  # a legacy 16-digit event id read as an offset
        "9" * 30,
        "5" * 5000,
    ],
)
def test_bad_last_event_id_is_a_real_400(env: Env, bad: str) -> None:
    env.seed()
    resp = env.get(leid=bad)
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/json")
    error = resp.json()["error"]
    assert error["code"] == "bad_request"
    assert error["retryable"] is False
    assert str(env.root) not in resp.text


@pytest.mark.parametrize("bad", ["٣", "٣٣", "5\n", "5\x00", "²", " 5", "5 ", "+5", "1_0", "-1", ""])
def test_parse_offset_accepts_only_ascii_digits(bad: str) -> None:
    with pytest.raises(InvalidOffset):
        event_follow.parse_offset(bad)


def test_parse_offset_accepts_plain_digits() -> None:
    assert event_follow.parse_offset("0") == 0
    assert event_follow.parse_offset("1234") == 1234


def test_last_event_id_sent_twice_is_a_400(env: Env) -> None:
    env.seed()
    resp = env.get(headers=[("Last-Event-ID", "0"), ("Last-Event-ID", "0")])
    assert resp.status_code == 400


def test_offset_beyond_end_of_log_is_a_400_not_a_skipped_backlog(env: Env) -> None:
    path = env.seed()
    resp = env.get(leid=str(path.stat().st_size + 1))
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "Last-Event-ID is beyond the end of the run's event log"
    assert env.get(leid="1000000").status_code == 400


def test_mid_line_offset_is_a_400(env: Env) -> None:
    path = env.seed()
    ends = line_ends(path)
    for offset in (1, ends[0] - 1, ends[0] + 1):
        assert env.get(leid=str(offset)).status_code == 400, offset


def test_offset_at_end_of_a_live_log_is_accepted(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.3")
    path = env.seed(finish=False)
    resp = env.get(leid=str(path.stat().st_size))
    assert resp.status_code == 200
    assert parse(resp.text) == []


def test_follow_flag_is_a_strict_bool(env: Env) -> None:
    env.seed()
    assert env.get(query="follow=2").status_code == 422
    assert env.get(query="follow=-1").status_code == 422


# --- after= is honoured with follow=1 and precedence is defined ----------------------


def test_after_is_translated_to_an_offset_when_following(env: Env) -> None:
    path = env.seed(n=3)
    log = [json.loads(line) for line in path.read_bytes().splitlines()]
    ends = line_ends(path)
    resp = env.get(query=f"follow=1&after={log[1]['event_id']}")
    assert resp.status_code == 200
    assert types(resp.text) == ["tick", "run.finished"]
    assert ids(resp.text) == ends[2:]


def test_unknown_after_is_a_400_when_following(env: Env) -> None:
    env.seed()
    assert env.get(query="follow=1&after=nope").status_code == 400
    assert env.get(query="follow=1&after=").status_code == 400


def test_last_event_id_wins_over_after(env: Env) -> None:
    path = env.seed(n=3)
    log = [json.loads(line) for line in path.read_bytes().splitlines()]
    ends = line_ends(path)
    resp = env.get(leid=str(ends[2]), query=f"follow=1&after={log[0]['event_id']}")
    assert ids(resp.text) == ends[3:]


def test_follow_and_backlog_responses_tell_proxies_not_to_buffer(env: Env) -> None:
    env.seed()
    for query in ("follow=1", "follow=0"):
        resp = env.get(query=query)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"


def test_shared_sse_headers_cannot_be_changed() -> None:
    with pytest.raises(TypeError):
        SSE_HEADERS["Cache-Control"] = "max-age=60"  # type: ignore[index]
    assert dict(SSE_HEADERS) == {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def test_backlog_mode_is_unchanged_and_ignores_last_event_id(env: Env) -> None:
    path = env.seed(n=2)
    log = [json.loads(line) for line in path.read_bytes().splitlines()]
    resp = env.get(query="follow=0", leid="7")
    assert resp.status_code == 200
    assert [f["id"] for f in parse(resp.text)] == [e["event_id"] for e in log]
    after = env.get(query=f"after={log[0]['event_id']}")
    assert [f["id"] for f in parse(after.text)] == [e["event_id"] for e in log[1:]]


# --- a half-written trailing line is read again, not dropped -------------------------


def test_half_written_line_is_delivered_once_it_completes(env: Env) -> None:
    path = env.log_path()
    path.parent.mkdir(parents=True)
    one, two, three = raw_line("a", "tick"), raw_line("b", "tick", {"pad": "x" * 40}), raw_line("c", "tick")
    path.write_bytes(one + two[:20])
    follower = EventFollower(path, 0)

    frames, done, _ = follower.poll()
    assert [json.loads(parse(f)[0]["data"])["event_id"] for f in frames] == ["a"]
    assert follower.offset == len(one)
    assert not done

    with path.open("ab") as f:
        f.write(two[20:] + three)
    frames, _, _ = follower.poll()
    assert [json.loads(parse(f)[0]["data"])["event_id"] for f in frames] == ["b", "c"]
    assert [int(parse(f)[0]["id"]) for f in frames] == [len(one + two), len(one + two + three)]


def test_follow_survives_a_line_written_in_pieces(env: Env) -> None:
    path = env.log_path()
    path.parent.mkdir(parents=True)
    one, two = raw_line("a", "tick"), raw_line("b", "tick", {"pad": "y" * 40})
    fin = raw_line("c", "run.finished", {"terminal": "solved"})
    path.write_bytes(one)

    def writer() -> None:
        with path.open("ab") as f:
            time.sleep(0.2)
            f.write(two[:25])
            f.flush()
            time.sleep(0.3)
            f.write(two[25:])
            f.flush()
            time.sleep(0.2)
            f.write(fin)

    thread = threading.Thread(target=writer)
    thread.start()
    resp = env.get()
    thread.join()
    assert [json.loads(f["data"])["event_id"] for f in parse(resp.text)] == ["a", "b", "c"]


# --- terminal by parsed event type ---------------------------------------------------


def _elapsed(env: Env, **kw: Any) -> tuple[float, Any]:
    started = time.monotonic()
    resp = env.get(**kw)
    return time.monotonic() - started, resp


def test_follow_ends_at_once_for_a_finished_run_not_held_in_memory(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    path = env.seed()
    assert ("t1", "r") not in env.app.state.recertia.runs
    elapsed, resp = _elapsed(env)
    assert resp.status_code == 200
    assert types(resp.text) == ["tick"] * 3 + ["run.finished"]
    assert ids(resp.text) == line_ends(path)
    assert elapsed < 2


def test_cancelled_run_ends_promptly(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.queued", {"mode": "async"})
    env.events.append("t1", "r", "run.cancelled", {"by": "operator"})
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.queued", "run.cancelled"]
    assert ids(resp.text) == line_ends(env.log_path())
    assert elapsed < 2


def test_failed_run_ends_promptly(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    env.events.append("t1", "r", "error", {"code": "run_failed", "message": "x", "retryable": False})
    env.events.append("t1", "r", "run.finished", {"terminal": "error"})
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.started", "error", "run.finished"]
    assert ids(resp.text) == line_ends(env.log_path())
    assert elapsed < 2


def test_retryable_error_does_not_end_the_follow(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.6")
    env.events.append("t1", "r", "run.queued", {})
    env.events.append("t1", "r", "error", {"code": "root_busy", "message": "x", "retryable": True})
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.queued", "error"]
    assert ids(resp.text) == line_ends(env.log_path())
    assert elapsed >= 0.5  # held open until the max follow duration


def test_lone_non_retryable_error_holds_until_the_max_duration(make_env: Callable[..., Env]) -> None:
    # A failed run always appends run.finished after its error; the error alone never ends it.
    env = make_env(RECERTIA_SSE_MAX_S="0.6")
    env.events.append("t1", "r", "run.started", {})
    env.events.append("t1", "r", "error", {"code": "run_failed", "message": "x", "retryable": False})
    elapsed, resp = _elapsed(env)
    assert resp.status_code == 200
    assert types(resp.text) == ["run.started", "error"]
    assert ids(resp.text) == line_ends(env.log_path())
    assert elapsed >= 0.5  # held open until the max follow duration


def test_a_terminal_key_or_text_in_a_payload_does_not_end_the_follow(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.6")
    env.events.append("t1", "r", "tick", {"terminal": "solved"})
    env.events.append("t1", "r", "tick", {"note": '"type":"run.finished"'})
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["tick", "tick"]
    assert ids(resp.text) == line_ends(env.log_path())
    assert elapsed >= 0.5


def test_events_after_a_terminal_event_are_still_delivered(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
    env.events.append("t1", "r", "run.queued", {"mode": "resume"})
    env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
    assert types(env.get().text) == ["run.finished", "run.queued", "run.finished"]


# --- a run the queue marks dead ------------------------------------------------------------


def _claim_for_a_worker(env: Env, run: str = "r", tenant: str = "t1") -> Any:
    """Queue the run with one attempt and claim it for a worker that reports this process's pid."""

    queue = env.app.state.recertia.queue
    queue.enqueue(QueueItem(tenant_id=tenant, run_id=run, kind="run", payload={}, max_attempts=1))
    queue.register_worker("w-follow", capacity=1, hostname="host", pid=os.getpid())
    assert len(queue.claim("w-follow")) == 1
    return queue


def _mark_dead(env: Env, monkeypatch: pytest.MonkeyPatch, run: str = "r", tenant: str = "t1") -> None:
    """The worker's process is gone on its last attempt, so reconciliation marks the claim dead."""

    queue = _claim_for_a_worker(env, run, tenant)
    with monkeypatch.context() as m:
        m.setattr(run_queue, "_pid_alive", lambda pid: False)
        assert queue.reconcile_orphans() == 1
    assert queue.get(tenant, run).status == "dead"


def _dead_frame(text: str) -> dict[str, Any]:
    last = parse(text)[-1]
    assert "id" not in last  # not a line of the log, so it carries no resume offset
    assert last["event"] == "run.finished"
    return dict(json.loads(last["data"]))


def test_dead_run_ends_with_a_terminal_event(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.queued", {"mode": "async"})
    env.events.append("t1", "r", "run.started", {})
    _mark_dead(env, monkeypatch)
    elapsed, resp = _elapsed(env)
    assert resp.status_code == 200
    assert types(resp.text) == ["run.queued", "run.started", "run.finished"]
    assert [int(f["id"]) for f in parse(resp.text)[:-1]] == line_ends(env.log_path())
    data = _dead_frame(resp.text)
    assert data["type"] == "run.finished"
    assert (data["run_id"], data["tenant_id"]) == ("r", "t1")
    assert data["payload"] == {"terminal": "error", "error_code": "lease_expired"}
    assert elapsed < 2  # not held until the max follow duration


def test_run_that_goes_dead_while_followed_ends_promptly(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    queue = _claim_for_a_worker(env)

    def worker_dies() -> None:
        time.sleep(0.4)
        env.events.append("t1", "r", "tick", {"n": 1})
        with monkeypatch.context() as m:
            m.setattr(run_queue, "_pid_alive", lambda pid: False)
            queue.reconcile_orphans()

    thread = threading.Thread(target=worker_dies)
    thread.start()
    elapsed, resp = _elapsed(env)
    thread.join()
    assert types(resp.text) == ["run.started", "tick", "run.finished"]
    assert _dead_frame(resp.text)["payload"]["terminal"] == "error"
    assert 0.3 <= elapsed < 3


def test_reconnect_to_a_dead_run_gets_only_the_terminal_event(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    _mark_dead(env, monkeypatch)
    end = str(env.log_path().stat().st_size)
    elapsed, resp = _elapsed(env, leid=end)
    assert types(resp.text) == ["run.finished"]
    _dead_frame(resp.text)
    assert elapsed < 2


def test_a_claimed_run_whose_worker_is_alive_still_holds(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.6")
    env.events.append("t1", "r", "run.started", {})
    queue = _claim_for_a_worker(env)
    assert queue.reconcile_orphans() == 0  # this process is alive, so the claim stays
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.started"]
    assert elapsed >= 0.5  # held open until the max follow duration


def test_a_queued_run_that_was_never_claimed_still_holds(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.6")
    env.events.append("t1", "r", "run.queued", {"mode": "async"})
    queue = env.app.state.recertia.queue
    queue.enqueue(QueueItem(tenant_id="t1", run_id="r", kind="run", payload={}))
    assert queue.get("t1", "r").status == "queued"
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.queued"]
    assert elapsed >= 0.5  # held open until the max follow duration


def test_a_run_requeued_after_its_worker_died_still_holds(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.6")
    env.events.append("t1", "r", "run.started", {})
    queue = env.app.state.recertia.queue
    queue.enqueue(QueueItem(tenant_id="t1", run_id="r", kind="run", payload={}, max_attempts=2))
    queue.register_worker("w-follow", capacity=1, hostname="host", pid=os.getpid())
    assert len(queue.claim("w-follow")) == 1
    with monkeypatch.context() as m:
        m.setattr(run_queue, "_pid_alive", lambda pid: False)
        assert queue.reconcile_orphans() == 1
    assert queue.get("t1", "r").status == "queued"  # an attempt is left, so it is not dead
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.started"]
    assert elapsed >= 0.5  # held open until the max follow duration


def test_dead_run_whose_log_ends_with_run_finished_gets_only_that_event(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker appended run.finished and died before it could record the outcome.
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
    _mark_dead(env, monkeypatch)
    for leid in (None, str(line_ends(env.log_path())[0])):
        elapsed, resp = _elapsed(env, leid=leid)
        frames = parse(resp.text)
        assert types(resp.text).count("run.finished") == 1
        assert json.loads(frames[-1]["data"])["payload"] == {"terminal": "solved"}
        assert int(frames[-1]["id"]) == env.log_path().stat().st_size
        assert elapsed < 2
    elapsed, resp = _elapsed(env, leid=str(env.log_path().stat().st_size))
    assert resp.text == ""
    assert elapsed < 2


@pytest.mark.parametrize("burst", [0, 1200])
def test_run_finished_written_just_before_the_worker_died_wins_over_the_dead_frame(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch, burst: int
) -> None:
    # The worker appends its last events (``burst`` ticks, more than one read when large) and
    # run.finished and dies, and the row is marked dead, after the follow has read the log
    # but before it reads the queue.
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    queue = _claim_for_a_worker(env)
    real_get = queue.get
    injected: list[bool] = []

    def get_after_the_worker_died(*args: Any, **kwargs: Any) -> Any:
        if not injected:
            injected.append(True)
            for i in range(burst):
                env.events.append("t1", "r", "tick", {"i": i, "pad": "x" * 1000})
            env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
            with monkeypatch.context() as m:
                m.setattr(run_queue, "_pid_alive", lambda pid: False)
                assert queue.reconcile_orphans() == 1
        return real_get(*args, **kwargs)

    monkeypatch.setattr(queue, "get", get_after_the_worker_died)
    elapsed, resp = _elapsed(env)
    assert injected == [True]
    assert queue.get("t1", "r").status == "dead"
    frames = parse(resp.text)
    assert types(resp.text) == ["run.started"] + ["tick"] * burst + ["run.finished"]
    assert json.loads(frames[-1]["data"])["payload"] == {"terminal": "solved"}
    assert [int(f["id"]) for f in frames] == line_ends(env.log_path())
    assert elapsed < 4
    elapsed, resp = _elapsed(env, leid=frames[-2]["id"])
    assert types(resp.text) == ["run.finished"]
    assert json.loads(parse(resp.text)[-1]["data"])["payload"] == {"terminal": "solved"}
    assert elapsed < 2


def test_dead_run_with_a_backlog_larger_than_one_read_gets_all_of_it_first(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    n = 1200
    for i in range(n):
        env.events.append("t1", "r", "tick", {"i": i, "pad": "x" * 1000})
    assert env.log_path().stat().st_size > event_follow.MAX_LINE_BYTES
    _mark_dead(env, monkeypatch)
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["tick"] * n + ["run.finished"]
    assert _dead_frame(resp.text)["payload"] == {"terminal": "error", "error_code": "lease_expired"}
    assert elapsed < 4


def test_dead_run_with_run_finished_then_a_torn_line_gets_only_that_event(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker wrote its own run.finished, then died part-way through one more line.
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
    with env.log_path().open("ab") as f:
        f.write(b'{"type":"tick","payl')
    _mark_dead(env, monkeypatch)
    elapsed, resp = _elapsed(env)
    frames = parse(resp.text)
    assert types(resp.text) == ["run.started", "run.finished"]
    assert json.loads(frames[-1]["data"])["payload"] == {"terminal": "solved"}
    assert [int(f["id"]) for f in frames] == line_ends(env.log_path())
    assert elapsed < 2


def test_a_finished_log_of_a_dead_run_never_reads_the_queue(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.seed(n=2)
    _mark_dead(env, monkeypatch)
    queue = env.app.state.recertia.queue
    real_get = queue.get
    calls: list[tuple[Any, ...]] = []

    def counting_get(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real_get(*args, **kwargs)

    monkeypatch.setattr(queue, "get", counting_get)
    resp = env.get()
    assert types(resp.text) == ["tick", "tick", "run.finished"]
    assert calls == []


# --- a run the queue marks failed without a terminal event in its log ----------------------


def _queued_run(env: Env, run: str = "r", tenant: str = "t1") -> Any:
    from recertia.workers.run_worker import dump_async_request, load_async_request

    tdir = env.root / "runs" / tenant
    req = load_async_request(
        {
            "run_id": run,
            "tenant_id": tenant,
            "request": run,
            "budget": {"max_attempts": 1},
            "workdir": str(env.root / "workspaces" / tenant / run),
            "skills_root": str(env.root / "skills"),
            "facts_root": str(env.root / "facts"),
            "runs_root": str(tdir),
            "index_path": str(tdir / "skill_index.db"),
        }
    )
    req.workdir.mkdir(parents=True, exist_ok=True)
    queue = env.app.state.recertia.queue
    queue.enqueue(QueueItem(tenant_id=tenant, run_id=run, kind="run", payload=dump_async_request(req)))
    return queue


def _fail_like(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """Make the real worker end the claim with ``code`` and no terminal event in the log."""

    from recertia.graph.engine import GraphOrchestrator
    from recertia.graph.lease import RunBusy
    from recertia.runtime.errors import WorkspaceBusy
    from recertia.workers import run_worker

    def busy(exc: Exception) -> Callable[..., Any]:
        def raise_it(*args: Any, **kwargs: Any) -> Any:
            raise exc

        return raise_it

    if code == "run_busy":
        monkeypatch.setattr(GraphOrchestrator, "start", busy(RunBusy("run r is busy")))
    elif code == "workspace_busy":
        monkeypatch.setattr(GraphOrchestrator, "start", busy(WorkspaceBusy("workspace busy")))
    else:  # a start-up failure: _run raises before its main try
        monkeypatch.setattr(run_worker, "build_default_orchestrator", busy(RuntimeError("no orchestrator")))


def _run_on_the_embedded_worker(env: Env, run: str = "r", tenant: str = "t1") -> Any:
    state = env.app.state.recertia
    claims = state.queue.claim(state.embedded.worker_id, limit=1)
    assert len(claims) == 1
    state.embedded._execute(claims[0])
    return state.queue.get(tenant, run)


@pytest.mark.parametrize("code", ["run_busy", "workspace_busy", "run_failed"])
def test_failed_run_with_no_terminal_event_ends_with_one(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    _queued_run(env)
    _fail_like(monkeypatch, code)
    row = _run_on_the_embedded_worker(env)
    assert (row.status, row.error_code) == ("failed", code)
    log = [json.loads(line) for line in env.log_path().read_bytes().splitlines()]
    assert [e["type"] for e in log] == ["run.started"]  # the worker wrote no terminal event
    elapsed, resp = _elapsed(env)
    assert resp.status_code == 200
    assert types(resp.text) == ["run.started", "run.finished"]
    assert int(parse(resp.text)[0]["id"]) == env.log_path().stat().st_size
    data = _dead_frame(resp.text)
    assert (data["run_id"], data["tenant_id"]) == ("r", "t1")
    assert data["payload"] == {"terminal": "error", "error_code": code}
    assert elapsed < 2  # not held until the max follow duration
    elapsed, resp = _elapsed(env, leid=str(env.log_path().stat().st_size))
    assert types(resp.text) == ["run.finished"]
    assert _dead_frame(resp.text)["payload"] == {"terminal": "error", "error_code": code}
    assert elapsed < 2


def test_failed_run_whose_log_ends_with_run_finished_gets_only_that_event(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.graph.engine import GraphOrchestrator

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("solver blew up")

    env = make_env(RECERTIA_SSE_MAX_S="5")
    _queued_run(env)
    monkeypatch.setattr(GraphOrchestrator, "start", boom)
    row = _run_on_the_embedded_worker(env)
    assert (row.status, row.error_code) == ("failed", "run_failed")
    elapsed, resp = _elapsed(env)
    frames = parse(resp.text)
    assert types(resp.text) == ["run.started", "error", "run.finished"]
    assert [int(f["id"]) for f in frames] == line_ends(env.log_path())
    assert json.loads(frames[-1]["data"])["payload"]["terminal"] == "error"
    assert elapsed < 2


# --- a run the queue ended cancelled or succeeded with no terminal event in its log ---------


def _standalone_run(env: Env, run: str = "r", *, cancel: str | None = None) -> Any:
    """Create the run through the API, then run it on the standalone worker (``recertia worker``).

    The standalone worker writes no run events, so the log holds only ``run.queued`` (and the
    ``run.cancelled`` of an operator cancel when ``cancel="route"``). ``cancel="queue"`` sets
    only the queue's cancel flag, so the log gets no terminal event at all.
    """

    from recertia.workers.serve import run_once

    state = env.app.state.recertia
    state.embedded.ensure_started = lambda: None  # only the standalone worker runs it
    created = env.client.post(
        "/v1/runs",
        headers={"X-API-Key": env.key1},
        json={"request": run, "run_id": run, "mode": "async", "budget": {"max_attempts": 1}},
    )
    assert created.status_code == 202, created.text
    if cancel == "route":
        resp = env.client.post(f"/v1/runs/{run}/cancel", headers={"X-API-Key": env.key1})
        assert resp.status_code == 200, resp.text
    elif cancel == "queue":
        state.queue.request_cancel("t1", run)
    assert run_once(env.root, worker="w-standalone") == 1
    return state.queue.get("t1", run)


def _queue_frame(text: str, status: str) -> dict[str, Any]:
    """The last frame is the queue's outcome, with exactly the ruled keys and no ``id:``."""

    data = _dead_frame(text)
    assert set(data) == {"run_id", "tenant_id", "type", "payload"}
    assert set(data["payload"]) == {"terminal", "error_code"}
    assert data["payload"]["terminal"] == ("error" if status in ("dead", "failed") else status)
    return data


@pytest.mark.parametrize(("cancel", "status"), [(None, "succeeded"), ("queue", "cancelled")])
def test_standalone_run_with_no_terminal_event_ends_with_the_queue_outcome(
    make_env: Callable[..., Env], cancel: str | None, status: str
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    row = _standalone_run(env, cancel=cancel)
    assert (row.status, row.error_code) == (status, None)
    log = [json.loads(line) for line in env.log_path().read_bytes().splitlines()]
    assert [e["type"] for e in log] == ["run.queued"]  # the standalone worker wrote no event
    elapsed, resp = _elapsed(env)
    assert resp.status_code == 200
    assert types(resp.text) == ["run.queued", "run.finished"]
    assert [int(f["id"]) for f in parse(resp.text)[:-1]] == line_ends(env.log_path())
    data = _queue_frame(resp.text, status)
    assert (data["run_id"], data["tenant_id"], data["type"]) == ("r", "t1", "run.finished")
    assert data["payload"] == {"terminal": status, "error_code": None}
    assert '"error_code": null' in resp.text and "None" not in resp.text
    assert elapsed < 2  # not held until the max follow duration
    elapsed, resp = _elapsed(env, leid=str(env.log_path().stat().st_size))
    assert types(resp.text) == ["run.finished"]
    assert _queue_frame(resp.text, status)["payload"] == {"terminal": status, "error_code": None}
    assert elapsed < 2


def test_succeeded_run_whose_log_ends_with_run_finished_gets_only_that_event(
    make_env: Callable[..., Env],
) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    row = _standalone_run(env)
    assert row.status == "succeeded"
    env.events.append("t1", "r", "run.finished", {"terminal": "solved"})
    for leid in (None, str(line_ends(env.log_path())[0])):
        elapsed, resp = _elapsed(env, leid=leid)
        frames = parse(resp.text)
        assert types(resp.text).count("run.finished") == 1
        assert json.loads(frames[-1]["data"])["payload"] == {"terminal": "solved"}
        assert int(frames[-1]["id"]) == env.log_path().stat().st_size  # the real event
        assert elapsed < 2
    elapsed, resp = _elapsed(env, leid=str(env.log_path().stat().st_size))
    assert resp.text == ""
    assert elapsed < 2


def test_cancelled_run_with_a_logged_cancel_ends_on_that_event(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="5")
    row = _standalone_run(env, cancel="route")
    assert row.status == "cancelled"
    elapsed, resp = _elapsed(env)
    frames = parse(resp.text)
    assert types(resp.text) == ["run.queued", "run.cancelled"]
    assert ids(resp.text) == line_ends(env.log_path())  # the last frame is the real, logged event
    assert "run.finished" not in resp.text
    assert elapsed < 2
    elapsed, resp = _elapsed(env, leid=frames[0]["id"])
    assert types(resp.text) == ["run.cancelled"]
    assert ids(resp.text) == [env.log_path().stat().st_size]
    assert elapsed < 2


def _end_row(env: Env, monkeypatch: pytest.MonkeyPatch, status: str, error_code: str | None) -> None:
    """End the queue row with ``status`` and ``error_code`` and write no terminal event."""

    if status == "dead":
        _mark_dead(env, monkeypatch)
        return
    queue = env.app.state.recertia.queue
    queue.enqueue(QueueItem(tenant_id="t1", run_id="r", kind="run", payload={}, max_attempts=1))
    queue.register_worker("w-follow", capacity=1, hostname="host", pid=os.getpid())
    [claim] = queue.claim("w-follow")
    queue.finish(claim, status=status, error_code=error_code)


@pytest.mark.parametrize(
    ("status", "error_code"),
    [("dead", "lease_expired"), ("failed", "run_failed"), ("cancelled", None), ("succeeded", None)],
)
def test_the_queue_end_frame_has_only_the_ruled_keys(
    make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch, status: str, error_code: str | None
) -> None:
    # Exact key sets, so an extra key (one that could carry a path) cannot ride on the frame.
    env = make_env(RECERTIA_SSE_MAX_S="5")
    env.events.append("t1", "r", "run.started", {})
    _end_row(env, monkeypatch, status, error_code)
    row = env.app.state.recertia.queue.get("t1", "r")
    assert (row.status, row.error_code) == (status, error_code)
    elapsed, resp = _elapsed(env)
    assert types(resp.text) == ["run.started", "run.finished"]
    data = _queue_frame(resp.text, status)
    assert data == {
        "run_id": "r",
        "tenant_id": "t1",
        "type": "run.finished",
        "payload": {"terminal": "error" if error_code else status, "error_code": error_code},
    }
    assert "None" not in resp.text  # a NULL error_code is JSON null, never the text None
    assert elapsed < 2


# --- max duration and keep-alive -----------------------------------------------------------


def test_follow_stops_at_the_configured_max_duration(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.5")
    env.seed(finish=False)
    elapsed, resp = _elapsed(env)
    assert resp.status_code == 200
    assert types(resp.text) == ["tick"] * 3
    assert 0.45 <= elapsed < 3


def test_idle_follow_sends_keep_alive_comments(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_SSE_MAX_S="0.6", RECERTIA_SSE_KEEPALIVE_S="0.1")
    env.seed(finish=False)
    resp = env.get()
    assert ": keep-alive\n\n" in resp.text
    assert types(resp.text) == ["tick"] * 3


@pytest.mark.parametrize(
    "name",
    [
        "RECERTIA_TENANT_MAX_SSE",
        "RECERTIA_GLOBAL_MAX_SSE",
        "RECERTIA_SSE_MAX_S",
        "RECERTIA_SSE_POLL_S",
        "RECERTIA_SSE_KEEPALIVE_S",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf"])
def test_invalid_follow_settings_are_rejected_at_startup(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        FollowSettings.from_env({name: value})


def test_follow_settings_defaults() -> None:
    assert FollowSettings.from_env({}) == FollowSettings(16, 64, 30.0, 0.5, 15.0)


# --- caps (in-process ASGI driver: streams stay open while others are made) ----------


class Conn:
    """One in-process request whose streamed body can be read while it is still open."""

    def __init__(self, app: Any, path: str, headers: dict[str, str], spec_version: str = "2.3") -> None:
        self.app, self.path, self.headers = app, path, headers
        self.spec_version = spec_version
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.body = b""
        self._started = asyncio.Event()
        self._disconnect = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> Conn:
        path, _, query = self.path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec_version},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver")]
            + [(k.lower().encode(), v.encode()) for k, v in self.headers.items()],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "state": {},
        }
        sent_request = False

        async def receive() -> dict[str, Any]:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self._disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if self._disconnect.is_set():
                if self.spec_version == "2.4" and message.get("body"):
                    raise OSError("client disconnected")  # an ASGI 2.4 server reports a closed socket
                return
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.response_headers = {
                    k.decode().lower(): v.decode() for k, v in message.get("headers", [])
                }
                self._started.set()
            elif message["type"] == "http.response.body":
                self.body += message.get("body", b"")

        async def run() -> None:
            with contextlib.suppress(OSError):  # a server drops the connection when the socket is gone
                await self.app(scope, receive, send)

        self._task = asyncio.create_task(run())
        await asyncio.wait_for(self._started.wait(), 10)
        return self

    async def read_until(self, needle: bytes, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self.body:
            assert time.monotonic() < deadline, self.body
            await asyncio.sleep(0.01)

    def disconnect(self) -> None:
        self._disconnect.set()

    async def closed(self, timeout: float = 5) -> None:
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout)


def follow_path(run: str = "r", query: str = "follow=1") -> str:
    return f"/v1/runs/{run}/events?{query}"


def test_per_tenant_cap_returns_429_and_other_tenants_are_unaffected(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_TENANT_MAX_SSE="2", RECERTIA_GLOBAL_MAX_SSE="10")
    env.seed(finish=False)
    env.seed(finish=False, tenant="t2")

    async def scenario() -> None:
        a = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        b = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        assert (a.status, b.status) == (200, 200)
        over = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        await over.closed()
        assert over.status == 429
        body = json.loads(over.body)["error"]
        assert body["code"] == "rate_limited" and body["retryable"] is True
        assert over.response_headers["retry-after"] == "1"
        other = await Conn(env.app, follow_path(), {"x-api-key": env.key2}).open()
        assert other.status == 200
        for conn in (a, b, other):
            conn.disconnect()
            await conn.closed()

    asyncio.run(scenario())


def test_global_cap_returns_429_across_tenants(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_TENANT_MAX_SSE="5", RECERTIA_GLOBAL_MAX_SSE="2")
    env.seed(finish=False)
    env.seed(finish=False, tenant="t2")

    async def scenario() -> None:
        a = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        b = await Conn(env.app, follow_path(), {"x-api-key": env.key2}).open()
        over = await Conn(env.app, follow_path(), {"x-api-key": env.key2}).open()
        await over.closed()
        assert (a.status, b.status, over.status) == (200, 200, 429)
        assert over.response_headers["retry-after"] == "1"
        for conn in (a, b):
            conn.disconnect()
            await conn.closed()

    asyncio.run(scenario())


def test_slot_is_released_on_disconnect_and_when_the_stream_ends(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_TENANT_MAX_SSE="1", RECERTIA_GLOBAL_MAX_SSE="1", RECERTIA_SSE_MAX_S="0.4")
    env.seed(finish=False)

    async def scenario() -> None:
        first = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        await first.read_until(b"event: tick")
        first.disconnect()
        await first.closed()
        second = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        assert second.status == 200
        await second.closed()  # ends by itself at the max duration
        third = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        assert third.status == 200
        third.disconnect()
        await third.closed()

    asyncio.run(scenario())


def test_slot_is_released_when_a_2_4_server_reports_the_closed_socket_on_send(
    make_env: Callable[..., Env],
) -> None:
    env = make_env(RECERTIA_TENANT_MAX_SSE="1", RECERTIA_GLOBAL_MAX_SSE="1", RECERTIA_SSE_KEEPALIVE_S="0.05")
    env.seed(finish=False)

    async def scenario() -> None:
        first = await Conn(env.app, follow_path(), {"x-api-key": env.key1}, spec_version="2.4").open()
        await first.read_until(b"event: tick")
        first.disconnect()
        await first.closed()  # the next keep-alive write fails and ends the stream
        again = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        assert again.status == 200
        again.disconnect()
        await again.closed()

    asyncio.run(scenario())


def test_rejected_requests_do_not_consume_a_slot(make_env: Callable[..., Env]) -> None:
    env = make_env(RECERTIA_TENANT_MAX_SSE="1", RECERTIA_GLOBAL_MAX_SSE="1")
    env.seed(finish=False)
    for _ in range(3):
        assert env.get(leid="abc").status_code == 400
        assert env.get("missing").status_code == 404

    async def scenario() -> None:
        conn = await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open()
        assert conn.status == 200
        conn.disconnect()
        await conn.closed()

    asyncio.run(scenario())


def test_ready_stays_responsive_with_many_open_follows(make_env: Callable[..., Env]) -> None:
    env = make_env(
        RECERTIA_TENANT_MAX_SSE="60",
        RECERTIA_GLOBAL_MAX_SSE="60",
        RECERTIA_SSE_MAX_S="20",
        RECERTIA_SSE_POLL_S="1",
    )
    env.seed(finish=False)

    async def scenario() -> None:
        conns = [await Conn(env.app, follow_path(), {"x-api-key": env.key1}).open() for _ in range(50)]
        assert {c.status for c in conns} == {200}
        started = time.monotonic()
        ready = await Conn(env.app, "/ready", {}).open()
        await ready.closed(timeout=5)
        elapsed = time.monotonic() - started
        assert ready.status in (200, 503)
        assert elapsed < 2
        for c in conns:
            c.disconnect()
        for c in conns:
            await c.closed()

    asyncio.run(scenario())


def test_limiter_counts_and_release_is_idempotent() -> None:
    limiter = FollowLimiter(tenant_max=1, global_max=2)
    release = limiter.acquire("a")
    with pytest.raises(event_follow.FollowCapExceeded) as tenant:
        limiter.acquire("a")
    assert tenant.value.scope == "tenant"
    other = limiter.acquire("b")
    with pytest.raises(event_follow.FollowCapExceeded) as overall:
        limiter.acquire("c")
    assert overall.value.scope == "global"
    release()
    release()
    assert limiter.open_count() == 1 and limiter.open_count("a") == 0
    other()
    assert limiter.open_count() == 0


# --- tenant isolation, authz before any read, path safety ----------------------------------


def test_other_tenant_gets_404_before_the_cursor_is_looked_at(env: Env) -> None:
    env.seed()
    for leid in (None, "0", "abc", "999999"):
        resp = env.get(key=env.key2, leid=leid)
        assert resp.status_code == 404
        assert "tick" not in resp.text


def test_tenant_header_cannot_reach_another_tenants_run(env: Env) -> None:
    env.seed()
    resp = env.get(key=env.key2, headers={"X-Recertia-Tenant": "t1"})
    assert resp.status_code == 403
    assert "tick" not in resp.text


def test_two_tenants_with_the_same_run_id_each_follow_only_their_own_log(env: Env) -> None:
    env.events.append("t1", "shared", "tick", {"who": "one"})
    env.events.append("t1", "shared", "run.finished", {"terminal": "solved"})
    env.events.append("t2", "shared", "tick", {"who": "two"})
    env.events.append("t2", "shared", "tick", {"who": "two-b"})
    env.events.append("t2", "shared", "run.finished", {"terminal": "solved"})
    one = env.get("shared", key=env.key1)
    two = env.get("shared", key=env.key2)
    assert [json.loads(f["data"])["tenant_id"] for f in parse(one.text)] == ["t1", "t1"]
    assert [json.loads(f["data"])["tenant_id"] for f in parse(two.text)] == ["t2"] * 3
    assert ids(one.text) == line_ends(env.log_path("shared", "t1"))
    assert ids(two.text) == line_ends(env.log_path("shared", "t2"))


@pytest.mark.parametrize("run", ["..", "..%2f..%2fsecret", "a%2f..%2fb", "r1%00", "%2e%2e"])
def test_run_id_path_tricks_never_reach_the_filesystem(env: Env, run: str) -> None:
    (env.root / "secret.jsonl").write_bytes(raw_line("s", "tick", {"leak": "yes"}))
    env.seed()
    resp = env.get(run, leid="0")
    assert resp.status_code in (400, 404)
    assert "leak" not in resp.text


def test_a_cursor_never_exposes_bytes_outside_complete_events(env: Env) -> None:
    path = env.log_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(
        raw_line("a", "tick", {"s": "é€😀"})
        + raw_line("b", "tick", {"s": "\n\u2028"})
        + raw_line("c", "run.finished")
    )
    size = path.stat().st_size
    valid = {0, *line_ends(path)}
    for offset in range(size + 3):
        if offset not in valid:
            with pytest.raises(InvalidOffset):
                env.events.follower("t1", "r", after_offset=offset)
            continue
        for frame in env.events.follow("t1", "r", after_offset=offset, max_seconds=1, poll_s=0.01):
            (block,) = parse(frame)
            assert json.loads(block["data"])["event_id"] in {"a", "b", "c"}


# --- hardening: framing, bounded reads, odd log content ------------------------------------


def test_event_type_cannot_inject_sse_fields(env: Env) -> None:
    path = env.log_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(
        raw_line("x", "a\ndata: injected\n\nevent: evil")
        + raw_line("y", "tick")
        + raw_line("z", "run.finished")
    )
    for query in ("follow=1", "follow=0"):
        resp = env.get(query=query)
        assert "injected\n" not in resp.text.replace('"a\\ndata: injected', "")
        assert len(parse(resp.text)) == 3
        assert [f["event"] for f in parse(resp.text)] == ["unknown", "tick", "run.finished"]


def test_lines_that_are_not_event_objects_are_skipped(env: Env) -> None:
    path = env.log_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b"[1]\n"
        + b'"abc"\n'
        + b"\n"
        + b"{broken\n"
        + b"\xff\xfe\n"
        + raw_line("a", "tick")
        + raw_line("b", "run.finished")
    )
    resp = env.get()
    assert resp.status_code == 200
    assert types(resp.text) == ["tick", "run.finished"]
    assert ids(resp.text) == line_ends(path)[-2:]


def test_an_over_long_line_is_skipped_without_losing_its_neighbours(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(event_follow, "MAX_LINE_BYTES", 256)
    path = env.log_path()
    path.parent.mkdir(parents=True)
    huge = raw_line("big", "tick", {"pad": "z" * 5000})
    path.write_bytes(raw_line("a", "tick") + huge + raw_line("b", "tick") + raw_line("c", "run.finished"))
    follower = EventFollower(path, 0)
    got: list[dict[str, str]] = []
    for _ in range(100):
        frames, done, more = follower.poll()
        got.extend(block for f in frames for block in parse(f))
        if done:
            break
    assert [json.loads(b["data"])["event_id"] for b in got] == ["a", "b", "c"]
    assert [int(b["id"]) for b in got] == [line_ends(path)[0], line_ends(path)[2], line_ends(path)[3]]


def test_follower_waits_for_a_log_that_does_not_exist_yet(tmp_path: Path) -> None:
    path = tmp_path / "later.jsonl"
    follower = EventFollower(path, 0)
    assert follower.poll() == ([], False, False)
    path.write_bytes(raw_line("a", "tick"))
    frames, done, _ = follower.poll()
    assert len(frames) == 1 and not done


def test_follow_ends_when_the_log_shrinks_below_the_cursor(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_bytes(raw_line("a", "tick") + raw_line("b", "tick"))
    follower = EventFollower(path, 0)
    assert len(follower.poll()[0]) == 2
    path.write_bytes(raw_line("c", "tick"))
    frames, done, _ = follower.poll()
    assert frames == [] and done


def test_windows_style_line_endings_keep_exact_offsets(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    lines = [raw_line("a", "tick")[:-1] + b"\r\n", raw_line("b", "run.finished")[:-1] + b"\r\n"]
    path.write_bytes(b"".join(lines))
    follower = EventFollower(path, 0)
    frames, done, _ = follower.poll()
    assert [int(parse(f)[0]["id"]) for f in frames] == [len(lines[0]), len(lines[0]) + len(lines[1])]
    assert done


def test_a_lone_carriage_return_in_the_type_is_sent_as_unknown() -> None:
    # A bare CR ends an SSE line, so it could start a new field just like LF.
    assert event_follow.event_name("a\rid: 9") == "unknown"
    assert event_follow.event_name("a\r") == "unknown"
    assert event_follow.event_name("\r") == "unknown"


def test_event_name_is_capped_at_128_characters() -> None:
    assert event_follow.event_name("a" * 128) == "a" * 128
    assert event_follow.event_name("a" * 129) == "unknown"


def test_terminal_event_with_unread_bytes_after_it_is_not_done(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    cancelled = raw_line("b", "run.cancelled")
    path.write_bytes(raw_line("a", "run.finished") + cancelled[:10])
    follower = EventFollower(path, 0)
    frames, done, _ = follower.poll()
    assert len(frames) == 1 and not done  # the half-written line must still be flushed
    with path.open("ab") as f:
        f.write(cancelled[10:])
    frames, done, _ = follower.poll()
    assert len(frames) == 1 and "run.cancelled" in frames[0] and done


class _CountingFollower(EventFollower):
    polls = 0

    def poll(self) -> tuple[list[str], bool, bool]:
        self.polls += 1
        return super().poll()


def test_idle_follow_sleeps_between_polls(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_bytes(raw_line("a", "tick"))
    follower = _CountingFollower(path, 0)
    settings = FollowSettings(max_seconds=0.5, poll_s=0.1, keepalive_s=100)

    async def drain() -> list[str]:
        return [frame async for frame in aiter_follow(follower, settings)]

    assert len(asyncio.run(drain())) == 1
    assert follower.polls <= 10, follower.polls


def test_backlog_larger_than_one_read_drains_without_waiting_for_a_poll(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    body = b"".join(raw_line(f"e{i}", "tick") for i in range(60000)) + raw_line("end", "run.finished")
    assert len(body) > 2 * event_follow.MAX_LINE_BYTES  # needs several bounded reads
    path.write_bytes(body)
    settings = FollowSettings(max_seconds=10, poll_s=60, keepalive_s=100)

    async def drain() -> list[str]:
        return [frame async for frame in aiter_follow(EventFollower(path, 0), settings)]

    started = time.monotonic()
    frames = asyncio.run(drain())
    elapsed = time.monotonic() - started
    assert len(frames) == 60001
    assert "run.finished" in frames[-1]
    assert elapsed < 5  # one read per poll would wait out poll_s before the second chunk


class _ScriptedFollower(EventFollower):
    """Returns the given ``(frames, done, more)`` passes in order, then idle passes."""

    def __init__(self, tmp_path: Path, passes: list[tuple[list[str], bool, bool]]) -> None:
        super().__init__(tmp_path / "unused.jsonl", 0)
        self.passes = passes
        self.polls = 0

    def poll(self) -> tuple[list[str], bool, bool]:
        self.polls += 1
        return self.passes.pop(0) if self.passes else ([], False, False)


def _tick(i: int) -> str:
    return f"id: {i}\nevent: tick\ndata: {{}}\n\n"


def test_ending_is_asked_only_once_the_follow_has_caught_up(tmp_path: Path) -> None:
    # Behind for three passes (more bytes each time), then caught up on the fourth.
    follower = _ScriptedFollower(
        tmp_path, [([_tick(1)], False, True), ([_tick(2)], False, True), ([_tick(3)], False, True)]
    )
    asked_at: list[int] = []

    def ending() -> str | None:
        asked_at.append(follower.polls)
        return "END"

    settings = FollowSettings(max_seconds=5, poll_s=0.01, keepalive_s=100)

    async def drain() -> list[str]:
        return [frame async for frame in aiter_follow(follower, settings, ending=ending)]

    frames = asyncio.run(drain())
    assert asked_at == [4]
    assert frames == [_tick(1), _tick(2), _tick(3), "END"]


def test_ending_is_not_asked_when_the_log_is_finished(tmp_path: Path) -> None:
    follower = _ScriptedFollower(tmp_path, [([_tick(1)], True, False)])
    asked: list[bool] = []

    def ending() -> str | None:
        asked.append(True)
        return "END"

    settings = FollowSettings(max_seconds=5, poll_s=0.01, keepalive_s=100)

    async def drain() -> list[str]:
        return [frame async for frame in aiter_follow(follower, settings, ending=ending)]

    assert asyncio.run(drain()) == [_tick(1)]
    assert asked == []


class _EndlessFollower(_ScriptedFollower):
    """Caught up on the first pass, then always has more (a writer that never stops)."""

    def poll(self) -> tuple[list[str], bool, bool]:
        self.polls += 1
        if self.polls == 1:
            return [], False, False
        time.sleep(0.005)
        return [_tick(self.polls)], False, self.polls < 400


def test_the_re_read_of_an_ended_run_stops_at_the_max_duration(tmp_path: Path) -> None:
    follower = _EndlessFollower(tmp_path, [])
    settings = FollowSettings(max_seconds=0.2, poll_s=0.01, keepalive_s=100)

    async def drain() -> list[str]:
        return [frame async for frame in aiter_follow(follower, settings, ending=lambda: "END")]

    started = time.monotonic()
    frames = asyncio.run(drain())
    elapsed = time.monotonic() - started
    assert "END" not in frames  # unread bytes remain, so the synthetic frame is not sent
    assert follower.polls < 200, follower.polls
    assert elapsed < 1.5
