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
        (PROXY, [PROXY]),  # even a trusted peer: no hop other than the peer remains
        (PROXY, [f"{PROXY}, {FLOODER}"]),  # --forwarded-allow-ips '*' takes the leftmost hop
    ],
)
def test_rewritten_peer_fails_closed(peer: str, xff: list[str]) -> None:
    for port in (None, 0, 40000):
        assert oidc_client_key(peer, xff, TRUSTED, port) == REWRITTEN
        assert oidc_client_key(peer, xff, (), port) == REWRITTEN


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


# ----- client-key handling -----

P, P2, V, C = PROXY, PROXY2, VICTIM, "192.0.2.44"


def _rewrite_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if "--no-proxy-headers" in r.getMessage() and "looks rewritten" in r.getMessage()
    ]


# A trusted proxy that appends itself is not a rewrite (port-guarded)


@pytest.mark.parametrize(
    "xff",
    [
        [f"{C}, {P}"],
        [C, P],
        [f"{V}, {C}, {P}"],
        [f"{C}, {P2}, {P}"],
        [f"{C}, {P}, {P}"],
        [f"{C}, ::ffff:{P}"],
    ],
)
def test_b1_trusted_proxy_that_appends_itself_is_not_a_rewrite(
    xff: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        assert oidc_client_key(P, xff, TRUSTED, 40000) == C
    assert _rewrite_warnings(caplog) == []
    assert oidc_client_key(P, xff, TRUSTED, 0) == REWRITTEN  # uvicorn rewrite: port 0
    assert oidc_client_key(P, xff, TRUSTED) == REWRITTEN  # port unknown: no skip


def test_b1_trusted_proxy_self_append_ipv6_and_mapped_peer() -> None:
    assert oidc_client_key("2001:db8:ffff::1", ["2001:db8:1:2::7, 2001:db8:ffff::1"], TRUSTED, 443) == (
        "2001:db8:1:2::/64"
    )
    assert oidc_client_key("::ffff:203.0.113.5", [f"{C}, {P}"], TRUSTED, 40000) == C


@pytest.mark.parametrize("xff", [[P], [f"{P}, {C}"], [f"{P}, {C}, {P}"], [P, C, P]])
def test_b1_trusted_peer_rewrite_shapes_still_fail_closed(xff: list[str]) -> None:
    # guards the '*' leftmost attack: the peer appears with no other hop, or left of one
    assert oidc_client_key(P, xff, TRUSTED, 40000) == REWRITTEN
    assert oidc_client_key(P, [f"{C}, {P}:443"], TRUSTED, 443) == REWRITTEN  # not a bare hop
    assert oidc_client_key(P, [f"{C}, [{P}]"], TRUSTED, 443) == REWRITTEN


def test_b1_self_append_skip_needs_a_trusted_peer() -> None:
    untrusted = parse_trusted_proxies(["2001:db8:ffff::/48"])
    assert oidc_client_key(P, [f"{C}, {P}"], untrusted, 40000) == REWRITTEN
    assert oidc_client_key(P, [f"{C}, {P}"], (), 40000) == REWRITTEN


def test_b1_route_trusted_proxy_appending_itself_keys_each_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, PROXY_NET)
    proxy = TestClient(_with_peer(app, P))  # peer port 40000
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        for i in range(40):
            r = proxy.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"198.51.100.{i}, {P}"})
            assert r.status_code == 200
    assert _keys(app) == {f"198.51.100.{i}" for i in range(40)}
    assert _rewrite_warnings(caplog) == []


@pytest.mark.parametrize("star", [False, True])
@pytest.mark.parametrize("trusted", ["", PROXY_NET])
def test_b1_uvicorn_rewrite_with_trusted_proxy_shapes_still_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, star: bool, trusted: str
) -> None:
    """Attacker shapes 'N, P' and 'P, N' through uvicorn's default and '*' give one key."""

    app = _oidc_app(tmp_path, monkeypatch, trusted)
    cfg: dict[str, Any] = {"forwarded_allow_ips": "*"} if star else {}
    victim = _uvicorn_client(app, VICTIM, **cfg)
    attacker = _uvicorn_client(app, FLOODER if star else "127.0.0.1", **cfg)
    v = victim.get("/v1/auth/oidc/login")
    assert v.status_code == 200
    for i in range(300):
        n = f"192.0.2.{i % 250}"
        fake = f"{n}, {P}" if i % 2 else f"{P}, {n}"
        assert attacker.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": fake}).status_code == 200
    sessions: SessionStore = app.state.recertia.sessions
    assert _keys(app) == {VICTIM, REWRITTEN}
    assert _pending_for(sessions, VICTIM) == [v.json()["state"]]


_REWRITE_SHAPES: list[tuple[list[str], bool]] = [
    ([C], True),
    ([f"{V}, {C}"], True),
    ([V, C], True),
    ([f"{C}, {P}"], True),
    (["2001:db8:1:2::7"], True),
    ([f"{C}:4711"], False),
    (["[2001:db8:1:2::7]:4711"], False),
]


@pytest.mark.parametrize("star", [False, True])
@pytest.mark.parametrize(("xff", "bare"), _REWRITE_SHAPES)
def test_b1_uvicorn_rewrite_port_contract(star: bool, xff: list[str], bare: bool) -> None:
    """Runs on the lowest (nightly py3.11, uvicorn floor) and newest (ci) uvicorn.

    The self-append skip relies on uvicorn giving a rewritten peer port 0 for a bare hop.
    If a bare-hop rewrite ever arrives with a non-zero port, this fails. A non-bare
    (``host:port``) hop may carry its port, but must still key as rewritten.
    """

    import uvicorn
    from fastapi.testclient import TestClient

    seen: list[tuple[str, int]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            seen.append((scope["client"][0], scope["client"][1]))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    cfg = uvicorn.Config(inner, log_config=None, forwarded_allow_ips="*" if star else None)
    cfg.load()
    peer = FLOODER if star else "127.0.0.1"
    client = TestClient(_with_peer(cfg.loaded_app, peer))
    assert client.get("/", headers=[("X-Forwarded-For", v) for v in xff]).status_code == 204
    host, port = seen[-1]
    assert host != peer  # uvicorn rewrote it
    if bare:
        assert port == 0, f"uvicorn rewrote {xff} to ({host}, {port}): the port-0 contract broke"
    every_hop = parse_trusted_proxies(["192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"])
    assert oidc_client_key(host, xff, every_hop, port) == REWRITTEN


# Bounded parsing (lines and bytes before splitting, hops right after);
# over a cap fails closed


@pytest.mark.parametrize("trusted", [TRUSTED, ()])
@pytest.mark.parametrize("peer", [PROXY, FLOODER])
@pytest.mark.parametrize(
    ("xff", "oversized"),
    [
        (["192.0.2.1"] * 16, False),
        (["192.0.2.1"] * 17, True),  # lines
        ([", ".join(["203.0.113.10"] * 64)], False),
        ([", ".join(["203.0.113.10"] * 65)], True),  # hops
        (["192.0.2.1," + " " * (8192 - 10)], False),
        (["192.0.2.1," + " " * (8192 - 9)], True),  # bytes
        (["192.0.2.1," + "2" * 8990], True),  # a 9000-byte value
        ([",".join(["1"] * 100_000)], True),
        (["x" * 1_000_000], True),
    ],
)
def test_b2_xff_parsing_is_bounded_and_fails_closed(
    peer: str, trusted: Any, xff: list[str], oversized: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        key = oidc_client_key(peer, xff, trusted, 40000)
    assert (key == REWRITTEN) is oversized
    assert bool([r for r in caplog.records if "parsing bounds" in r.getMessage()]) is oversized


def test_b2_oversized_xff_is_rejected_before_splitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = console_auth._hop_ip_loose

    def spy(hop: str) -> Any:
        calls.append(hop)
        return real(hop)

    monkeypatch.setattr(console_auth, "_hop_ip_loose", spy)
    assert oidc_client_key(FLOODER, [",".join(["192.0.2.1"] * 50_000)], ()) == REWRITTEN
    assert oidc_client_key(FLOODER, ["x" * 1_000_000], ()) == REWRITTEN  # one huge hop
    assert oidc_client_key(FLOODER, ["192.0.2.1"] * 17, ()) == REWRITTEN  # many short lines
    assert calls == []


_NASTY = [
    "",
    ",",
    ",,,,",
    " , ",
    "[",
    "]:",
    "[::1",
    "[2001:db8::1]:x",
    ":::",
    "unix:",
    "192.0.2.4%",
    "2001:db8::1%eth0",
    "::ffff:192.0.2.1%x",
    "999.1.1.1",
    "192.0.2.1:99999999999999999999",
    "١٩٢.0.2.1",
    "\t192.0.2.1\t",
    "\u00ff\u00fe",
    "for=192.0.2.1",
    "unknown",
    "_hidden",
]


@pytest.mark.parametrize("peer", [PROXY, FLOODER, "2001:db8::1%eth0", "::ffff:198.51.100.7%1", "not-an-ip"])
def test_a_malformed_xff_never_raises(peer: str) -> None:
    for value in _NASTY:
        for xff in ([value], [value, value], [VICTIM, value], [value, PROXY]):
            assert isinstance(oidc_client_key(peer, xff, TRUSTED), str)
            assert isinstance(oidc_client_key(peer, xff, ()), str)


@pytest.mark.parametrize("trusted", ["", PROXY_NET])
@pytest.mark.parametrize("peer", [PROXY, FLOODER, "127.0.0.1"])
def test_a_route_malformed_or_oversized_xff_never_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str, peer: str
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, trusted)
    client = TestClient(_with_peer(app, peer), raise_server_exceptions=False)
    cases: list[list[tuple[bytes, bytes]]] = [[(b"X-Forwarded-For", v.encode("utf-8"))] for v in _NASTY]
    cases += [
        [(b"X-Forwarded-For", b"\xff\xfe\x80")],  # not valid UTF-8
        [(b"X-Forwarded-For", b"192.0.2.1")] * 40,  # many lines
        [(b"X-Forwarded-For", b", ".join([b"192.0.2.1"] * 500))],  # many hops
        [(b"X-Forwarded-For", b"1" * 8000)],
        [(b"X-Forwarded-For", b"1" * 9000)],  # over the byte cap
    ]
    for headers in cases:
        r = client.get("/v1/auth/oidc/login", headers=headers)
        assert r.status_code == 200, (headers[:1], r.status_code)


# Warning once per process


def test_a_warnings_fire_once_per_process_across_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        for n in range(3):  # three app instances in one process
            app = _oidc_app(tmp_path / str(n), monkeypatch)
            untrusted = TestClient(_with_peer(app, FLOODER))
            rewritten = TestClient(_with_peer(app, VICTIM))
            for i in range(5):
                untrusted.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": f"192.0.2.{i}"})
                rewritten.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": VICTIM})
                untrusted.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": "1" * 9000})
            for _ in range(PER_CLIENT + 3):  # fill the shared bucket
                rewritten.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": VICTIM})
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("RECERTIA_TRUSTED_PROXIES is required" in m for m in msgs) == 1
    assert sum("looks rewritten" in m for m in msgs) == 1
    assert sum("parsing bounds" in m for m in msgs) == 1
    assert sum("is at its 16-start limit" in m for m in msgs) == 1


def test_a_warning_once_under_threads(caplog: pytest.LogCaptureFixture) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"), ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda i: oidc_client_key(FLOODER, [f"192.0.2.{i % 200}"], ()), range(2000)))
    assert sum("RECERTIA_TRUSTED_PROXIES" in r.getMessage() for r in caplog.records) == 1


def test_b1_rewrite_warning_is_loud_about_the_one_bucket_case(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        oidc_client_key("127.0.0.1", ["127.0.0.1"], ())
    (msg,) = [r.getMessage() for r in caplog.records]
    assert "EVERY" in msg and "ONE" in msg and "loopback" in msg and "every user" in msg
    go_live = (REPO / "docs" / "architecture" / "go-live.md").read_text(encoding="utf-8")
    assert "every user lands in one shared 16-slot bucket" in " ".join(go_live.split())


# An untrusted peer's XFF cannot reach another client's entries or budget


@pytest.mark.parametrize("trusted", ["", PROXY_NET])
def test_a_untrusted_xff_cannot_touch_another_clients_entries_or_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch, trusted)
    sessions: SessionStore = app.state.recertia.sessions
    victim = TestClient(_with_peer(app, VICTIM))
    attacker = TestClient(_with_peer(app, FLOODER))
    mine = [victim.get("/v1/auth/oidc/login").json()["state"] for _ in range(3)]
    forged = [
        VICTIM,
        f"{VICTIM}, {VICTIM}",
        f"192.0.2.1, {VICTIM}",
        f"{VICTIM}:4711",
        "[198.51.100.7]",
        f"{VICTIM}, {PROXY}",
        f"{PROXY}, {VICTIM}",
        "198.51.100.0/24",
    ]
    for i in range(400):
        lines = [forged[i % len(forged)]] + ([VICTIM] if i % 3 == 0 else [])
        r = attacker.get("/v1/auth/oidc/login", headers=[("X-Forwarded-For", v) for v in lines])
        assert r.status_code == 200
    assert _keys(app) == {VICTIM, FLOODER}  # the attacker never got the victim's key
    assert _pending_for(sessions, VICTIM) == mine
    # Budget untouched: the victim can still add 13 starts without losing any of its own.
    more = [victim.get("/v1/auth/oidc/login").json()["state"] for _ in range(PER_CLIENT - 3)]
    assert _pending_for(sessions, VICTIM) == mine + more


# A self-XFF client lands in the shared bucket and evicts nothing outside it


def test_b1_self_xff_client_only_evicts_inside_the_shared_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    app = _oidc_app(tmp_path, monkeypatch)
    sessions: SessionStore = app.state.recertia.sessions
    victims = {f"198.51.100.{n}": TestClient(_with_peer(app, f"198.51.100.{n}")) for n in (7, 8, 9)}
    before = {
        ip: [c.get("/v1/auth/oidc/login").json()["state"] for _ in range(4)] for ip, c in victims.items()
    }
    me = "192.0.2.50"
    selfxff = TestClient(_with_peer(app, me))
    for _ in range(1100):
        assert selfxff.get("/v1/auth/oidc/login", headers={"X-Forwarded-For": me}).status_code == 200
    assert _keys(app) == {*victims, REWRITTEN}
    assert len(_pending_for(sessions, REWRITTEN)) == PER_CLIENT
    for ip, states in before.items():
        assert _pending_for(sessions, ip) == states


def test_b1_shared_bucket_at_its_limit_evicts_only_its_own_on_a_full_table() -> None:
    store = _store()
    others = [
        store.begin_oidc(redirect_uri=CB, client_key=f"198.51.100.{128 + i % 64}")[0]
        for i in range(PENDING_MAX - PER_CLIENT)
    ]
    bucket = [store.begin_oidc(redirect_uri=CB, client_key=REWRITTEN)[0] for _ in range(PER_CLIENT)]
    assert len(store._oidc) == PENDING_MAX
    for _ in range(500):
        bucket.append(store.begin_oidc(redirect_uri=CB, client_key=REWRITTEN)[0])
    assert all(s in store._oidc for s in others)
    assert _pending_for(store, REWRITTEN) == bucket[-PER_CLIENT:]


# Multi-line X-Forwarded-For through uvicorn


@pytest.mark.parametrize("star", [False, True])
@pytest.mark.parametrize("trusted", ["", PROXY_NET])
def test_s1_uvicorn_multi_line_xff_still_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, star: bool, trusted: str
) -> None:
    app = _oidc_app(tmp_path, monkeypatch, trusted)
    cfg: dict[str, Any] = {"forwarded_allow_ips": "*"} if star else {}
    victim = _uvicorn_client(app, VICTIM, **cfg)
    attacker = _uvicorn_client(app, FLOODER if star else "127.0.0.1", **cfg)
    v = victim.get("/v1/auth/oidc/login")
    for i in range(300):
        lines = [("X-Forwarded-For", f"198.51.100.{i % 200 + 10}"), ("X-Forwarded-For", f"192.0.2.{i % 250}")]
        assert attacker.get("/v1/auth/oidc/login", headers=lines).status_code == 200
    assert _keys(app) == {VICTIM, REWRITTEN}
    assert app.state.recertia.sessions.take_oidc(v.json()["state"]) is not None


# Both warnings in one process, exactly once each


def test_s2_both_warnings_fire_once_each_in_one_process(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        for i in range(50):
            oidc_client_key(FLOODER, [f"192.0.2.{i}"], ())  # untrusted XFF
            oidc_client_key(f"192.0.2.{i}", [f"192.0.2.{i}"], ())  # rewrite
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("RECERTIA_TRUSTED_PROXIES is required" in m for m in msgs) == 1
    assert sum("looks rewritten" in m for m in msgs) == 1
    assert len(msgs) == 2


# The shared bucket filling up is logged once


def test_s3_shared_bucket_full_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    store = _store()
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        for _ in range(PER_CLIENT):
            store.begin_oidc(redirect_uri=CB, client_key=REWRITTEN)
        assert caplog.records == []  # full, but nothing evicted yet
        for _ in range(PER_CLIENT):
            store.begin_oidc(redirect_uri=CB, client_key=VICTIM)  # an ordinary client at its limit
        store.begin_oidc(redirect_uri=CB, client_key=VICTIM)
        assert caplog.records == []
        for _ in range(100):
            store.begin_oidc(redirect_uri=CB, client_key=REWRITTEN)
    (msg,) = [r.getMessage() for r in caplog.records]
    assert "proxy-headers-rewritten" in msg and "16-start limit" in msg and "--no-proxy-headers" in msg
    go_live = " ".join((REPO / "docs" / "architecture" / "go-live.md").read_text(encoding="utf-8").split())
    assert "all logins fail intermittently behind a proxy" in go_live


# Wording (X-Forwarded-For is inspected for detection, used for the key only when trusted)


def test_s4_docs_say_used_for_the_key_only_when_trusted() -> None:
    go_live = " ".join((REPO / "docs" / "architecture" / "go-live.md").read_text(encoding="utf-8").split())
    changelog = " ".join((REPO / "CHANGELOG.md").read_text(encoding="utf-8").split())
    settings_src = " ".join((REPO / "src" / "recertia" / "settings.py").read_text(encoding="utf-8").split())
    assert "`X-Forwarded-For` is used for the client key only when the peer is inside" in go_live
    assert "`X-Forwarded-For` is read only when" not in go_live
    assert "use `X-Forwarded-For` for the client key only from a peer" in changelog
    assert "forwarded headers are never read" not in settings_src
    assert "X-Forwarded-For is never used for the client key" in settings_src


# The untrusted-peer warning uses the same wording


def test_na_untrusted_xff_warning_says_used_for_the_key_only_when_trusted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        assert oidc_client_key(FLOODER, ["192.0.2.1"], TRUSTED, 40000) == FLOODER
    (msg,) = [r.getMessage() for r in caplog.records]
    assert "not using X-Forwarded-For for the client key (untrusted peer)" in msg
    assert "it is used for the key only when the peer is trusted" in msg
    assert "RECERTIA_TRUSTED_PROXIES is required" in msg
    assert "ignoring" not in msg


# Docs say lines/bytes are capped before the split and hops right after


def test_nb_docs_describe_where_each_xff_cap_runs() -> None:
    go_live = " ".join((REPO / "docs" / "architecture" / "go-live.md").read_text(encoding="utf-8").split())
    changelog = " ".join((REPO / "CHANGELOG.md").read_text(encoding="utf-8").split())
    doc = " ".join((console_auth.oidc_client_key.__doc__ or "").split())
    assert (
        "header lines and bytes are checked before the header is split (16 lines, 8192 bytes) "
        "and the hop count right after the split (64 hops)" in go_live
    )
    assert "16 lines and 8192 bytes are checked before splitting, 64 hops right after" in changelog
    assert "is checked before the header is split, and more than ``_XFF_MAX_HOPS`` hops right after" in doc
    for text in (go_live, changelog, doc):
        assert "bounded before the header is split (16 header lines, 8192 bytes, 64 hops)" not in text
        assert "bounded before splitting (16 lines, 8192 bytes, 64 hops" not in text
        assert "checked before the header is split: more than" not in text


# Further checks: trusted-proxy settings, warnings, login state expiry


def test_n2_trusted_proxies_accept_host_bits() -> None:
    got = load_settings({"RECERTIA_TRUSTED_PROXIES": "192.0.2.1/24, 2001:db8:ffff::1/48"}).trusted_proxies
    assert got == ("192.0.2.0/24", "2001:db8:ffff::/48")


def test_n3_untrusted_peer_without_xff_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="recertia.api.console_auth"):
        assert oidc_client_key(FLOODER, [], TRUSTED, 40000) == FLOODER
    assert caplog.records == []


def test_n4_expired_state_cannot_be_redeemed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(console_auth, "time", SimpleNamespace(time=lambda: now[0]))
    store = _store()
    state = store.begin_oidc(redirect_uri=CB, client_key=VICTIM)[0]
    now[0] += console_auth._OIDC_TTL_S + 1
    assert store.take_oidc(state) is None
