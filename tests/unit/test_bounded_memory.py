"""In-process buffer caps (REC-056a, #131): telemetry deques and the pending OIDC map.

The pending OIDC map is fed by unauthenticated ``GET /v1/auth/oidc/login`` calls, so it is
bounded overall (1024) and per client (16). A client is the ASGI peer address the login
route sees (``request.client.host``); forwarded-for headers are not read by the app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recertia.api.console_auth import SessionStore
from recertia.telemetry import Telemetry

pytest.importorskip("fastapi")

PER_CLIENT = 16
PENDING_MAX = 1024


def _store() -> SessionStore:
    return SessionStore(secret="k" * 32)


def _pending_for(store: SessionStore, client_key: str) -> list[str]:
    return [k for k, v in store._oidc.items() if v.client_key == client_key]


def test_telemetry_deque_and_oidc_cap() -> None:
    tel = Telemetry()
    for i in range(1500):
        tel.emit("policy.changed", tenant_id="t1", seq=i)
        with tel.span(f"span-{i}", tenant_id="t1"):
            pass
    assert len(tel.events) == 1024
    assert len(tel.spans) == 1024
    # Oldest evicted first: the survivors are the newest 1024, in order.
    assert tel.events[0].attributes["seq"] == 1500 - 1024
    assert tel.events[-1].attributes["seq"] == 1499
    assert tel.spans[0].name == f"span-{1500 - 1024}"
    assert tel.spans[-1].name == "span-1499"

    # 2000 starts spread over 125 clients (16 each, at the per-client limit) overflow the map.
    store = _store()
    for i in range(2000):
        store.begin_oidc(redirect_uri="https://app.example/cb", client_key=f"10.0.{i % 125}.1")
    assert len(store._oidc) == PENDING_MAX
    # 2000 starts from one client never hold more than its per-client limit.
    single = _store()
    for _ in range(2000):
        single.begin_oidc(redirect_uri="https://app.example/cb", client_key="10.9.9.9")
    assert len(single._oidc) == PER_CLIENT


def test_one_client_past_its_limit_cannot_evict_another_clients_login() -> None:
    store = _store()
    victim_state, victim_verifier, _ = store.begin_oidc(
        redirect_uri="https://app.example/cb", client_key="198.51.100.7"
    )
    flood = [
        store.begin_oidc(redirect_uri="https://app.example/cb", client_key="203.0.113.66")[0]
        for _ in range(5000)
    ]
    # The flooder is held to its per-client limit and only loses its own oldest starts.
    assert _pending_for(store, "203.0.113.66") == flood[-PER_CLIENT:]
    assert len(store._oidc) == PER_CLIENT + 1
    taken = store.take_oidc(victim_state)
    assert taken is not None
    assert taken.verifier == victim_verifier


def test_flood_from_many_clients_does_not_block_or_evict_a_light_client() -> None:
    store = _store()
    victim_state, _, _ = store.begin_oidc(redirect_uri="https://app.example/cb", client_key="198.51.100.7")
    # 100 flooding clients, each at its limit: 1600 starts against a 1024 map.
    for i in range(1600):
        store.begin_oidc(redirect_uri="https://app.example/cb", client_key=f"203.0.{i % 100}.1")
    assert len(store._oidc) == PENDING_MAX
    assert victim_state in store._oidc
    # A new client can still start (and finish) a login while the map is full.
    late_state, _, _ = store.begin_oidc(redirect_uri="https://app.example/cb", client_key="192.0.2.10")
    assert len(store._oidc) == PENDING_MAX
    assert store.take_oidc(late_state) is not None
    assert store.take_oidc(victim_state) is not None


def _with_peer(app: Any, host: str) -> Any:
    """Present ``host`` as the ASGI peer address (portable across Starlette versions)."""

    async def wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": (host, 40000)}
        await app(scope, receive, send)

    return wrapped


def test_login_route_keys_pending_starts_by_peer_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("RECERTIA_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_SECRET", "csecret")
    app = create_app(root=tmp_path / "api-root")
    sessions: SessionStore = app.state.recertia.sessions
    victim = TestClient(_with_peer(app, "198.51.100.7"))
    flooder = TestClient(_with_peer(app, "203.0.113.66"))

    start = victim.get("/v1/auth/oidc/login")
    assert start.status_code == 200
    victim_state = start.json()["state"]
    for i in range(40):
        # A forged forwarded-for header does not change the flooder's client key.
        r = flooder.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"10.1.{i}.1"})
        assert r.status_code == 200
    assert len(_pending_for(sessions, "203.0.113.66")) == PER_CLIENT
    assert _pending_for(sessions, "198.51.100.7") == [victim_state]
    assert sessions.take_oidc(victim_state) is not None
