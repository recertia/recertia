"""REL-6 F25: in-process collections stay capped."""

from __future__ import annotations

from recertia.api.console_auth import SessionStore
from recertia.telemetry import Telemetry


def test_telemetry_deque_and_oidc_cap() -> None:
    tel = Telemetry()
    for i in range(5000):
        tel.emit("policy.changed", tenant_id="t1", n=i)
    assert len(tel.events) == 1024
    store = SessionStore(secret="x" * 32)
    for _ in range(2000):
        store.begin_oidc(redirect_uri="http://127.0.0.1/cb")
    assert len(store._oidc) == 1024
