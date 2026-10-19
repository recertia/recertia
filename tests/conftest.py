"""Explicit test-only execution capability for integration fixtures.

Production defaults to the Docker/Podman backend.  Tests opt into the bounded
local executor before importing or constructing orchestration services.
"""

from __future__ import annotations

import os

os.environ.setdefault("RECERTIA_EXECUTION_BACKEND", "local")
# Tests exercise the HTTP API against the local backend; production API refuses local
# unless this break-glass flag is set.
os.environ.setdefault("RECERTIA_API_ALLOW_LOCAL_EXEC", "1")
# CI / non-Windows hosts: allow POSIX absolute registered roots (RW0 tests).
os.environ.setdefault("RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS", "1")
# Busy-lock import waits then raises. Keep the wait short in the suite.
os.environ.setdefault("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")


# --- Repo-wide no-external-network guard (PR5) --------------------------------------
# Installed for the whole session at configure time, so it also covers import-time and
# fixture-time connections, not just test bodies. Always on in CI (CI=true or
# RECERTIA_NO_NETWORK=1); locally, RECERTIA_NO_NETWORK=0 opts out for ad-hoc debugging.
# Loopback and AF_UNIX stay allowed (TestClient, local servers, Docker socket).

import ipaddress  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}
_REAL: dict[str, Any] = {
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "getaddrinfo": socket.getaddrinfo,
    "create_connection": socket.create_connection,
}


class NetworkBlockedError(OSError):
    """A test tried to reach a non-loopback host (OSError so urllib wraps it as URLError)."""


def network_guard_enabled() -> bool:
    if os.environ.get("CI", "").lower() == "true":
        return True
    return os.environ.get("RECERTIA_NO_NETWORK", "1") != "0"


def _is_loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None or str(host).lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check_addr(sock: socket.socket, addr: Any) -> None:
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return
    host = addr[0] if isinstance(addr, tuple) and addr else addr
    if not _is_loopback(host):
        raise NetworkBlockedError(f"external network blocked in tests: {addr!r}")


def _guarded_connect(self: socket.socket, addr: Any) -> None:
    _check_addr(self, addr)
    return _REAL["connect"](self, addr)


def _guarded_connect_ex(self: socket.socket, addr: Any) -> int:
    _check_addr(self, addr)
    return _REAL["connect_ex"](self, addr)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_loopback(host):
        raise NetworkBlockedError(f"external DNS lookup blocked in tests: {host!r}")
    return _REAL["getaddrinfo"](host, *args, **kwargs)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    if not _is_loopback(address[0]):
        raise NetworkBlockedError(f"external network blocked in tests: {address!r}")
    return _REAL["create_connection"](address, *args, **kwargs)


def pytest_configure(config: Any) -> None:
    if not network_guard_enabled():
        return
    setattr(socket.socket, "connect", _guarded_connect)
    setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    setattr(socket, "create_connection", _guarded_create_connection)


def pytest_unconfigure(config: Any) -> None:
    setattr(socket.socket, "connect", _REAL["connect"])
    setattr(socket.socket, "connect_ex", _REAL["connect_ex"])
    setattr(socket, "getaddrinfo", _REAL["getaddrinfo"])
    setattr(socket, "create_connection", _REAL["create_connection"])


_REL3_GUARD_FILES = (
    "tests/unit/graph/test_rel3_probe.py",
    "tests/unit/graph/test_fenced_persistence.py",
    "tests/unit/persistence/test_import_legacy.py",
    "tests/probes/rel3_",
)
_rel3_skip_xfail: list[str] = []


def _rel3_guard_match(nodeid: str) -> bool:
    normalized = nodeid.replace("\\", "/")
    return any(path in normalized for path in _REL3_GUARD_FILES)


def pytest_runtest_logreport(report: Any) -> None:
    """Fail the session if a REL-3 probe or regression test skipped or xfailed."""

    nodeid = getattr(report, "nodeid", "")
    if not _rel3_guard_match(nodeid):
        return
    wasxfail = bool(getattr(report, "wasxfail", False))
    if report.outcome == "skipped" or wasxfail:
        kind = "xfail" if wasxfail else "skipped"
        _rel3_skip_xfail.append(f"{kind}: {nodeid}")


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not _rel3_skip_xfail:
        return
    seen = list(dict.fromkeys(_rel3_skip_xfail))
    sys.stderr.write("REL-3 skip/xfail guard failed:\n")
    for line in seen:
        sys.stderr.write(f"  {line}\n")
    if exitstatus == 0:
        session.exitstatus = 1


def pytest_runtest_setup(item: Any) -> None:
    """Each test starts with a closed model circuit so failures do not leak."""

    del item
    from recertia.solver.resilience import default_breaker

    default_breaker().reset()
