"""In-process buffer caps (REC-056a, #131): telemetry deques and the pending OIDC map.

The pending OIDC map is fed by unauthenticated ``GET /v1/auth/oidc/login`` calls, so it is
bounded per client (16) with a global backstop (1024, oldest evicted). The client is the
socket peer; ``X-Forwarded-For`` is read only when that peer is a configured trusted proxy
(``RECERTIA_TRUSTED_PROXIES``), and then the rightmost hop that is not a trusted proxy wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recertia.api import console_auth
from recertia.api.console_auth import SessionStore, oidc_client_key, parse_trusted_proxies
from recertia.errors import ConfigError
from recertia.settings import load_settings
from recertia.telemetry import Telemetry

pytest.importorskip("fastapi")

PER_CLIENT = 16
PENDING_MAX = 1024
CB = "https://app.example/cb"
PROXY = "10.0.0.5"
TRUSTED = parse_trusted_proxies(["10.0.0.0/24", "fd00::/8"])


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
        store.begin_oidc(redirect_uri=CB, client_key=f"10.0.{i % 125}.1")
    assert len(store._oidc) == PENDING_MAX
    # 2000 starts from one client never hold more than its per-client limit.
    single = _store()
    for _ in range(2000):
        single.begin_oidc(redirect_uri=CB, client_key="10.9.9.9")
    assert len(single._oidc) == PER_CLIENT


def test_one_client_past_its_limit_cannot_evict_another_clients_login() -> None:
    store = _store()
    victim_state, victim_verifier, _ = store.begin_oidc(redirect_uri=CB, client_key="198.51.100.7")
    flood = [store.begin_oidc(redirect_uri=CB, client_key="203.0.113.66")[0] for _ in range(5000)]
    # The flooder is held to its per-client limit and only loses its own oldest starts.
    assert _pending_for(store, "203.0.113.66") == flood[-PER_CLIENT:]
    assert len(store._oidc) == PER_CLIENT + 1
    taken = store.take_oidc(victim_state)
    assert taken is not None
    assert taken.verifier == victim_verifier
    # The flooder did not block the victim's next login either.
    again, _, _ = store.begin_oidc(redirect_uri=CB, client_key="198.51.100.7")
    assert store.take_oidc(again) is not None


def test_path1_full_table_over_limit_client_evicts_only_its_own_oldest() -> None:
    # Per-client check first: a client at its own limit drops its OWN oldest start, even
    # when the table is full and the globally oldest entry belongs to another client.
    store = _store()
    other = store.begin_oidc(redirect_uri=CB, client_key="198.51.100.7")[0]  # globally oldest
    mine = [store.begin_oidc(redirect_uri=CB, client_key="203.0.113.66")[0] for _ in range(PER_CLIENT)]
    for i in range(PENDING_MAX - PER_CLIENT - 1):
        store.begin_oidc(redirect_uri=CB, client_key=f"192.0.{i % 64}.1")
    assert len(store._oidc) == PENDING_MAX
    before = set(store._oidc) - set(mine)
    new_state = store.begin_oidc(redirect_uri=CB, client_key="203.0.113.66")[0]
    assert len(store._oidc) == PENDING_MAX
    assert other in store._oidc
    assert set(store._oidc) - set(mine) - {new_state} == before  # other clients intact
    assert _pending_for(store, "203.0.113.66") == [*mine[1:], new_state]


def test_c_full_table_evicts_globally_oldest_and_accepts_new_start() -> None:
    store = _store()
    states = [
        store.begin_oidc(redirect_uri=CB, client_key=f"203.0.{i % 64}.1")[0] for i in range(PENDING_MAX)
    ]
    assert len(store._oidc) == PENDING_MAX
    new_state, new_verifier, _ = store.begin_oidc(redirect_uri=CB, client_key="192.0.2.10")
    assert len(store._oidc) == PENDING_MAX
    assert states[0] not in store._oidc  # oldest evicted
    assert all(s in store._oidc for s in states[1:])  # nothing else
    taken = store.take_oidc(new_state)
    assert taken is not None
    assert taken.verifier == new_verifier


def test_e_per_client_limit_below_global_cap() -> None:
    assert console_auth._OIDC_PENDING_PER_CLIENT == PER_CLIENT
    assert console_auth._OIDC_PENDING_MAX == PENDING_MAX
    assert 0 < PER_CLIENT < PENDING_MAX
    console_auth._validate_oidc_limits(PER_CLIENT, PENDING_MAX)
    for per_client, total in [(PENDING_MAX, PENDING_MAX), (PENDING_MAX + 1, PENDING_MAX), (0, PENDING_MAX)]:
        with pytest.raises(ValueError, match="per_client"):
            console_auth._validate_oidc_limits(per_client, total)


# ----- client key: socket peer, trusted-proxy X-Forwarded-For -----


def test_b_client_key_ignores_xff_without_trusted_proxies() -> None:
    assert oidc_client_key("203.0.113.66", ["198.51.100.7"], ()) == "203.0.113.66"


def test_b_client_key_ignores_xff_from_untrusted_peer() -> None:
    assert oidc_client_key("203.0.113.66", ["198.51.100.7"], TRUSTED) == "203.0.113.66"


def test_a_client_key_takes_rightmost_untrusted_hop() -> None:
    # A client can prepend anything; only hops appended by trusted proxies count.
    xff = ["1.1.1.1, 198.51.100.7", "10.0.0.9"]
    assert oidc_client_key(PROXY, xff, TRUSTED) == "198.51.100.7"
    assert oidc_client_key("fd00::1", ["2001:db8::7"], TRUSTED) == "2001:db8::7"


@pytest.mark.parametrize(
    "xff",
    [
        [],
        [""],
        ["not-an-ip"],
        ["198.51.100.7, garbage"],
        ["198.51.100.7,,"],
        ["198.51.100.7:4711"],
        ["10.0.0.7, 10.0.0.8"],  # entirely trusted proxies
    ],
)
def test_d_absent_malformed_or_all_trusted_xff_falls_back_to_peer(xff: list[str]) -> None:
    assert oidc_client_key(PROXY, xff, TRUSTED) == PROXY


def test_d_client_key_without_peer_is_unknown() -> None:
    assert oidc_client_key(None, ["198.51.100.7"], TRUSTED) == "unknown"


def test_trusted_proxies_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings({}).trusted_proxies == ()
    got = load_settings({"RECERTIA_TRUSTED_PROXIES": " 10.0.0.0/24 , 192.0.2.1,fd00::/8 "})
    assert got.trusted_proxies == ("10.0.0.0/24", "192.0.2.1/32", "fd00::/8")
    with pytest.raises(ConfigError, match="RECERTIA_TRUSTED_PROXIES"):
        load_settings({"RECERTIA_TRUSTED_PROXIES": "10.0.0.0/24,proxy.internal"})


# ----- route level -----


def _with_peer(app: Any, host: str) -> Any:
    """Present ``host`` as the socket peer (portable across Starlette versions)."""

    async def wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": (host, 40000)}
        await app(scope, receive, send)

    return wrapped


def _oidc_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str = "") -> Any:
    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("RECERTIA_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_SECRET", "csecret")
    if trusted:
        monkeypatch.setenv("RECERTIA_TRUSTED_PROXIES", trusted)
    else:
        monkeypatch.delenv("RECERTIA_TRUSTED_PROXIES", raising=False)
    return create_app(root=tmp_path / "api-root")


@pytest.mark.parametrize("trusted", ["", "10.0.0.0/24"])
def test_b_spoofed_xff_from_untrusted_peer_cannot_dodge_or_exhaust_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, trusted)
    sessions: SessionStore = app.state.recertia.sessions
    victim = TestClient(_with_peer(app, "198.51.100.7"))
    flooder = TestClient(_with_peer(app, "203.0.113.66"))

    start = victim.get("/v1/auth/oidc/login")
    assert start.status_code == 200
    victim_state = start.json()["state"]
    for i in range(40):
        # Rotating fake hops would dodge the limit; claiming the victim's address would
        # exhaust the victim's. Neither works from a peer that is not a trusted proxy.
        fake = "198.51.100.7" if i % 2 else f"10.1.{i}.1"
        r = flooder.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": fake})
        assert r.status_code == 200
    assert len(_pending_for(sessions, "203.0.113.66")) == PER_CLIENT
    assert _pending_for(sessions, "198.51.100.7") == [victim_state]
    assert sessions.take_oidc(victim_state) is not None


def test_b_loopback_peer_xff_ignored_when_no_trusted_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch)
    sessions: SessionStore = app.state.recertia.sessions
    local = TestClient(_with_peer(app, "127.0.0.1"))
    for i in range(20):
        r = local.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"198.51.100.{i}"})
        assert r.status_code == 200
    assert len(_pending_for(sessions, "127.0.0.1")) == PER_CLIENT
    assert {v.client_key for v in sessions._oidc.values()} == {"127.0.0.1"}


def test_b_documented_uvicorn_launch_keeps_the_loopback_socket_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """go-live.md launches with --no-proxy-headers; uvicorn's default would rewrite the peer."""

    import uvicorn
    from fastapi.testclient import TestClient

    go_live = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "go-live.md"
    launch = [
        ln for ln in go_live.read_text(encoding="utf-8").splitlines() if "uvicorn recertia.api.app:app" in ln
    ]
    assert launch and all("--no-proxy-headers" in ln for ln in launch)

    def key_seen(proxy_headers: bool) -> set[str]:
        app = _oidc_app(tmp_path / str(proxy_headers), monkeypatch)
        config = uvicorn.Config(app, proxy_headers=proxy_headers, log_config=None)
        config.load()
        client = TestClient(_with_peer(config.loaded_app, "127.0.0.1"))
        r = client.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": "198.51.100.7"})
        assert r.status_code == 200
        return {v.client_key for v in app.state.recertia.sessions._oidc.values()}

    assert key_seen(proxy_headers=False) == {"127.0.0.1"}  # documented launch
    assert key_seen(proxy_headers=True) == {"198.51.100.7"}  # uvicorn default: the hazard


def test_d_forwarded_header_ignored_even_from_trusted_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, "10.0.0.0/24")
    sessions: SessionStore = app.state.recertia.sessions
    proxy = TestClient(_with_peer(app, PROXY))
    assert proxy.get("/v1/auth/oidc/login", headers={"Forwarded": "for=198.51.100.7"}).status_code == 200
    assert proxy.get("/v1/auth/oidc/login").status_code == 200  # no XFF at all
    assert proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": "bogus"}).status_code == 200
    assert {v.client_key for v in sessions._oidc.values()} == {PROXY}


def test_a_two_clients_behind_trusted_proxy_are_limited_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, "10.0.0.0/24")
    sessions: SessionStore = app.state.recertia.sessions
    proxy = TestClient(_with_peer(app, PROXY))

    start = proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": "198.51.100.7"})
    assert start.status_code == 200
    a_state = start.json()["state"]
    for _ in range(40):
        # B forges a leftmost hop; the proxy appends B's real address on the right.
        r = proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.66"})
        assert r.status_code == 200
    assert len(_pending_for(sessions, "203.0.113.66")) == PER_CLIENT
    assert _pending_for(sessions, "198.51.100.7") == [a_state]
    assert _pending_for(sessions, PROXY) == []
    assert sessions.take_oidc(a_state) is not None
