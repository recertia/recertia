"""In-process buffer caps (REC-056a, #131): telemetry deques and the pending OIDC map.

The pending OIDC map is fed by unauthenticated ``GET /v1/auth/oidc/login`` calls, so it is
bounded per client (16) with a global backstop (1024, oldest evicted). The client is the
socket peer; ``X-Forwarded-For`` is read only when that peer is a configured trusted proxy
(``RECERTIA_TRUSTED_PROXIES``), and then the rightmost hop that is not a trusted proxy wins.
A peer that an outer proxy-headers layer rewrote is detected and fails closed.

Addresses are documentation ranges only (RFC 5737, RFC 3849) plus loopback.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
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
REWRITTEN = "proxy-headers-rewritten"
PROXY_NET = "203.0.113.0/28"
PROXY = "203.0.113.5"
PROXY2 = "203.0.113.9"
VICTIM = "198.51.100.7"
FLOODER = "203.0.113.66"
TRUSTED = parse_trusted_proxies([PROXY_NET, "2001:db8:ffff::/48"])
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_auth, "_warned", set())


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
        store.begin_oidc(redirect_uri=CB, client_key=f"192.0.2.{i % 125}")
    assert len(store._oidc) == PENDING_MAX
    # 2000 starts from one client never hold more than its per-client limit.
    single = _store()
    for _ in range(2000):
        single.begin_oidc(redirect_uri=CB, client_key="192.0.2.200")
    assert len(single._oidc) == PER_CLIENT


def test_begin_oidc_requires_a_client_key() -> None:
    with pytest.raises(TypeError, match="client_key"):
        _store().begin_oidc(redirect_uri=CB)  # type: ignore[call-arg]


def test_one_client_past_its_limit_cannot_evict_another_clients_login() -> None:
    store = _store()
    victim_state, victim_verifier, _ = store.begin_oidc(redirect_uri=CB, client_key=VICTIM)
    flood = [store.begin_oidc(redirect_uri=CB, client_key=FLOODER)[0] for _ in range(5000)]
    # The flooder is held to its per-client limit and only loses its own oldest starts.
    assert _pending_for(store, FLOODER) == flood[-PER_CLIENT:]
    assert len(store._oidc) == PER_CLIENT + 1
    taken = store.take_oidc(victim_state)
    assert taken is not None
    assert taken.verifier == victim_verifier
    # The flooder did not block the victim's next login either.
    again, _, _ = store.begin_oidc(redirect_uri=CB, client_key=VICTIM)
    assert store.take_oidc(again) is not None


def test_path1_full_table_over_limit_client_evicts_only_its_own_oldest() -> None:
    # Per-client check first: a client at its own limit drops its OWN oldest start, even
    # when the table is full and the globally oldest entry belongs to another client.
    store = _store()
    other = store.begin_oidc(redirect_uri=CB, client_key=VICTIM)[0]  # globally oldest
    mine = [store.begin_oidc(redirect_uri=CB, client_key=FLOODER)[0] for _ in range(PER_CLIENT)]
    for i in range(PENDING_MAX - PER_CLIENT - 1):
        store.begin_oidc(redirect_uri=CB, client_key=f"192.0.2.{128 + i % 64}")
    assert len(store._oidc) == PENDING_MAX
    before = set(store._oidc) - set(mine)
    new_state = store.begin_oidc(redirect_uri=CB, client_key=FLOODER)[0]
    assert len(store._oidc) == PENDING_MAX
    assert other in store._oidc
    assert set(store._oidc) - set(mine) - {new_state} == before  # other clients intact
    assert _pending_for(store, FLOODER) == [*mine[1:], new_state]


def test_c_full_table_evicts_globally_oldest_and_accepts_new_start() -> None:
    store = _store()
    states = [
        store.begin_oidc(redirect_uri=CB, client_key=f"198.51.100.{128 + i % 64}")[0]
        for i in range(PENDING_MAX)
    ]
    assert len(store._oidc) == PENDING_MAX
    new_state, new_verifier, _ = store.begin_oidc(redirect_uri=CB, client_key="192.0.2.10")
    assert len(store._oidc) == PENDING_MAX
    assert states[0] not in store._oidc  # oldest evicted
    assert all(s in store._oidc for s in states[1:])  # nothing else
    taken = store.take_oidc(new_state)
    assert taken is not None
    assert taken.verifier == new_verifier


def test_expired_starts_are_purged_before_either_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(console_auth, "time", SimpleNamespace(time=lambda: now[0]))
    store = _store()
    for _ in range(PER_CLIENT):
        store.begin_oidc(redirect_uri=CB, client_key=VICTIM)
    now[0] += console_auth._OIDC_TTL_S + 1
    fresh = store.begin_oidc(redirect_uri=CB, client_key=VICTIM)[0]
    assert _pending_for(store, VICTIM) == [fresh]  # expired starts did not count

    full = _store()
    for i in range(PENDING_MAX - 1):
        full.begin_oidc(redirect_uri=CB, client_key=f"198.51.100.{128 + i % 64}")
    now[0] += 500
    live = full.begin_oidc(redirect_uri=CB, client_key=VICTIM)[0]
    now[0] += 150  # the first 1023 are now expired, ``live`` is not
    new = full.begin_oidc(redirect_uri=CB, client_key="192.0.2.10")[0]
    assert set(full._oidc) == {live, new}  # purged first; no live entry evicted


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
    assert oidc_client_key(FLOODER, [VICTIM], ()) == FLOODER


def test_b_client_key_ignores_xff_from_untrusted_peer() -> None:
    assert oidc_client_key(FLOODER, [VICTIM], TRUSTED) == FLOODER


def test_b_untrusted_xff_warns_once_naming_trusted_proxies(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        for i in range(5):
            assert oidc_client_key(FLOODER, [f"192.0.2.{i}"], ()) == FLOODER
        assert oidc_client_key(FLOODER, [], ()) == FLOODER
    hits = [r for r in caplog.records if "RECERTIA_TRUSTED_PROXIES" in r.getMessage()]
    assert len(hits) == 1


def test_a_client_key_takes_rightmost_untrusted_hop() -> None:
    # A client can prepend anything; only hops appended by trusted proxies count.
    xff = ["192.0.2.99, 198.51.100.7", PROXY2]
    assert oidc_client_key(PROXY, xff, TRUSTED) == VICTIM
    # IPv6 clients are keyed by their /64.
    assert oidc_client_key("2001:db8:ffff::1", ["2001:db8:1:2::7"], TRUSTED) == "2001:db8:1:2::/64"


def test_a_every_xff_line_is_combined_in_order() -> None:
    # HAProxy-style: the client forges a first line, the trusted proxy adds its own line.
    assert oidc_client_key(PROXY, [VICTIM, "192.0.2.44"], TRUSTED) == "192.0.2.44"
    assert oidc_client_key(PROXY, [VICTIM, "192.0.2.44", PROXY2], TRUSTED) == "192.0.2.44"


def test_ipv4_mapped_peers_and_hops_are_folded_to_ipv4() -> None:
    assert oidc_client_key("::ffff:203.0.113.5", [VICTIM], TRUSTED) == VICTIM  # trusted proxy
    assert oidc_client_key("::ffff:198.51.100.7", [], TRUSTED) == VICTIM
    assert oidc_client_key(PROXY, ["::ffff:192.0.2.44"], TRUSTED) == "192.0.2.44"


def test_ipv6_keys_are_grouped_by_64() -> None:
    keys = {oidc_client_key(f"2001:db8:1:2::{i:x}", [], ()) for i in range(1, 200)}
    assert keys == {"2001:db8:1:2::/64"}
    assert oidc_client_key("2001:db8:1:3::1", [], ()) == "2001:db8:1:3::/64"
    store = _store()
    for i in range(1, 41):
        store.begin_oidc(redirect_uri=CB, client_key=oidc_client_key(f"2001:db8:1:2::{i:x}", [], ()))
    assert len(store._oidc) == PER_CLIENT


@pytest.mark.parametrize(
    "xff",
    [
        [],
        [""],
        ["not-an-ip"],
        ["198.51.100.7, garbage"],
        ["198.51.100.7,,"],
        ["198.51.100.7:4711"],
        ["[2001:db8::7]"],
        [f"{PROXY2}, 203.0.113.10"],  # entirely trusted proxies
    ],
)
def test_d_absent_malformed_or_all_trusted_xff_falls_back_to_peer(xff: list[str]) -> None:
    assert oidc_client_key(PROXY, xff, TRUSTED) == PROXY


def test_d_client_key_without_peer_is_unknown() -> None:
    assert oidc_client_key(None, [VICTIM], TRUSTED) == "unknown"
    assert oidc_client_key("", [], ()) == "unknown"


@pytest.mark.parametrize(
    ("peer", "xff"),
    [
        ("testclient", []),  # not an IP: something replaced the socket address
        ("unix:/run/sock", [VICTIM]),
        (VICTIM, [VICTIM]),  # peer equals an XFF hop: uvicorn proxy headers rewrote it
        (VICTIM, ["192.0.2.1, 198.51.100.7"]),
        (VICTIM, ["198.51.100.7:4711"]),
        ("2001:db8::1", ["[2001:db8::1]:443"]),
        (PROXY, [PROXY]),  # even a trusted peer
    ],
)
def test_rewritten_peer_fails_closed(peer: str, xff: list[str]) -> None:
    assert oidc_client_key(peer, xff, TRUSTED) == REWRITTEN
    assert oidc_client_key(peer, xff, ()) == REWRITTEN


def test_rewrite_warns_once_naming_the_flag(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        for i in range(5):
            assert oidc_client_key(f"192.0.2.{i}", [f"192.0.2.{i}"], ()) == REWRITTEN
    hits = [r for r in caplog.records if "--no-proxy-headers" in r.getMessage()]
    assert len(hits) == 1


def test_trusted_proxies_setting() -> None:
    assert load_settings({}).trusted_proxies == ()
    got = load_settings({"RECERTIA_TRUSTED_PROXIES": " 203.0.113.0/28 , 192.0.2.1,2001:db8:ffff::/48 "})
    assert got.trusted_proxies == ("203.0.113.0/28", "192.0.2.1/32", "2001:db8:ffff::/48")
    with pytest.raises(ConfigError, match="RECERTIA_TRUSTED_PROXIES"):
        load_settings({"RECERTIA_TRUSTED_PROXIES": "203.0.113.0/28,proxy.internal"})


@pytest.mark.parametrize("value", ["0.0.0.0/0", "::/0", "192.0.2.1, 0.0.0.0/0"])
def test_trusted_proxies_refuses_trust_everything(value: str) -> None:
    with pytest.raises(ConfigError, match="trusts every address"):
        load_settings({"RECERTIA_TRUSTED_PROXIES": value})


def test_every_documented_launch_line_has_no_proxy_headers() -> None:
    files = [
        REPO / "AGENTS.md",
        REPO / "README.md",
        *(REPO / "docs").rglob("*.md"),
        *(REPO / "src").rglob("*.py"),
    ]
    launch = [
        (f.relative_to(REPO).as_posix(), ln)
        for f in files
        if f.is_file()
        for ln in f.read_text(encoding="utf-8").splitlines()
        if "uvicorn recertia.api.app:app" in ln
    ]
    where = {path for path, _ in launch}
    assert {"AGENTS.md", "docs/architecture/go-live.md", "src/recertia/api/app.py"} <= where
    assert [x for x in launch if "--no-proxy-headers" not in x[1]] == []


# ----- route level -----


def _with_peer(app: Any, host: str | None) -> Any:
    """Present ``host`` as the socket peer (``None`` = no peer, as under ``--uds``)."""

    async def wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "client": None if host is None else (host, 40000)}
        await app(scope, receive, send)

    return wrapped


def _oidc_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str = "") -> Any:
    from recertia.api import create_app

    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("RECERTIA_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("RECERTIA_OIDC_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    if trusted:
        monkeypatch.setenv("RECERTIA_TRUSTED_PROXIES", trusted)
    else:
        monkeypatch.delenv("RECERTIA_TRUSTED_PROXIES", raising=False)
    return create_app(root=tmp_path / "api-root")


def _keys(app: Any) -> set[str]:
    return {v.client_key for v in app.state.recertia.sessions._oidc.values()}


@pytest.mark.parametrize("trusted", ["", PROXY_NET])
def test_b_spoofed_xff_from_untrusted_peer_cannot_dodge_or_exhaust_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, trusted)
    sessions: SessionStore = app.state.recertia.sessions
    victim = TestClient(_with_peer(app, VICTIM))
    flooder = TestClient(_with_peer(app, FLOODER))

    start = victim.get("/v1/auth/oidc/login")
    assert start.status_code == 200
    victim_state = start.json()["state"]
    for i in range(40):
        # Rotating fake hops would dodge the limit; claiming the victim's address would
        # exhaust the victim's. Neither works from a peer that is not a trusted proxy.
        fake = VICTIM if i % 2 else f"192.0.2.{i}"
        r = flooder.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": fake})
        assert r.status_code == 200
    assert len(_pending_for(sessions, FLOODER)) == PER_CLIENT
    assert _pending_for(sessions, VICTIM) == [victim_state]
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
    assert _keys(app) == {"127.0.0.1"}


def _uvicorn_client(app: Any, peer: str, **config: Any) -> Any:
    import uvicorn
    from fastapi.testclient import TestClient

    cfg = uvicorn.Config(app, log_config=None, **config)
    cfg.load()
    return TestClient(_with_peer(cfg.loaded_app, peer))


def test_b_documented_uvicorn_launch_keeps_the_loopback_socket_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _oidc_app(tmp_path, monkeypatch)
    client = _uvicorn_client(app, "127.0.0.1", proxy_headers=False)  # --no-proxy-headers
    assert client.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": VICTIM}).status_code == 200
    assert _keys(app) == {"127.0.0.1"}


def test_uvicorn_default_proxy_headers_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag forgotten: uvicorn rewrites loopback peers from XFF. Must not mint keys or evict."""

    app = _oidc_app(tmp_path, monkeypatch)
    victim = _uvicorn_client(app, VICTIM)  # uvicorn defaults: proxy headers on, trusts loopback
    attacker = _uvicorn_client(app, "127.0.0.1")
    v = victim.get("/v1/auth/oidc/login")
    assert v.status_code == 200
    for i in range(1100):
        r = attacker.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"192.0.2.{i % 250}"})
        assert r.status_code == 200
    sessions: SessionStore = app.state.recertia.sessions
    assert _keys(app) == {VICTIM, REWRITTEN}
    assert len(_pending_for(sessions, REWRITTEN)) == PER_CLIENT
    assert sessions.take_oidc(v.json()["state"]) is not None


def test_uvicorn_forwarded_allow_ips_star_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--forwarded-allow-ips '*': a remote peer's XFF is trusted by uvicorn; we fail closed."""

    app = _oidc_app(tmp_path, monkeypatch)
    victim = _uvicorn_client(app, "127.0.0.1", forwarded_allow_ips="*")
    attacker = _uvicorn_client(app, FLOODER, forwarded_allow_ips="*")
    v = victim.get("/v1/auth/oidc/login")
    assert v.status_code == 200
    for i in range(1100):
        fake = f"198.51.100.{i % 250}, 192.0.2.{i % 250}"
        r = attacker.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": fake})
        assert r.status_code == 200
    sessions: SessionStore = app.state.recertia.sessions
    assert _keys(app) == {"127.0.0.1", REWRITTEN}
    assert len(_pending_for(sessions, REWRITTEN)) == PER_CLIENT
    assert sessions.take_oidc(v.json()["state"]) is not None


def test_d_unix_socket_peer_shares_unknown_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch)
    uds = TestClient(_with_peer(app, None))
    for i in range(20):
        assert uds.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code == 200
    assert _keys(app) == {"unknown"}
    assert len(app.state.recertia.sessions._oidc) == PER_CLIENT


def test_d_forwarded_header_ignored_even_from_trusted_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, PROXY_NET)
    proxy = TestClient(_with_peer(app, PROXY))
    assert proxy.get("/v1/auth/oidc/login", headers={"Forwarded": f"for={VICTIM}"}).status_code == 200
    assert proxy.get("/v1/auth/oidc/login").status_code == 200  # no XFF at all
    assert proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": "bogus"}).status_code == 200
    assert _keys(app) == {PROXY}


def test_a_two_clients_behind_trusted_proxy_are_limited_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, PROXY_NET)
    sessions: SessionStore = app.state.recertia.sessions
    proxy = TestClient(_with_peer(app, PROXY))

    start = proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": VICTIM})
    assert start.status_code == 200
    a_state = start.json()["state"]
    for _ in range(40):
        # B forges a leftmost hop; the proxy appends B's real address on the right.
        r = proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"{VICTIM}, {FLOODER}"})
        assert r.status_code == 200
    assert len(_pending_for(sessions, FLOODER)) == PER_CLIENT
    assert _pending_for(sessions, VICTIM) == [a_state]
    assert _pending_for(sessions, PROXY) == []
    assert sessions.take_oidc(a_state) is not None


def test_a_route_combines_separate_xff_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, PROXY_NET)
    proxy = TestClient(_with_peer(app, PROXY))
    headers = [("X-Forwarded-For", VICTIM), ("X-Forwarded-For", "192.0.2.44")]
    assert proxy.get("/v1/auth/oidc/login", headers=headers).status_code == 200
    assert _keys(app) == {"192.0.2.44"}
