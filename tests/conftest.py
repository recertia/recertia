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


def pytest_runtest_setup(item: Any) -> None:
    """Each test starts with a closed model circuit so failures do not leak."""

    del item
    from recertia.solver.resilience import default_breaker

    default_breaker().reset()


def pytest_runtest_teardown(item: Any, nextitem: Any) -> None:
    """Stop every worker thread a test started (REC-029, N4, S1, S2).

    Apps built with ``create_app`` start a ``recertia-queue`` daemon thread on
    the first create or resume and nothing stopped it, so threads piled up
    across the suite. This runs before fixture finalizers, so no queue is
    closed under a live worker thread. A worker restarts on its next
    ``ensure_started`` (create, resume or ``flush``); a test that waits on the
    queue across a test boundary without one of those would stall.
    """

    del item, nextitem
    stop_test_workers()


def stop_test_workers(timeout: float = 5.0) -> None:
    """Stop embedded queue workers and join idle async pool threads.

    Fails the test when a queue worker is still alive after ``timeout``: it
    would leak into later tests (S2). Async pools are shut down either way so
    no ``recertia-run`` thread survives into a later fork (S1).
    """

    import pytest

    from recertia.workers.embedded import EmbeddedQueueWorker
    from recertia.workers.run_worker import AsyncRunWorker

    stopped = EmbeddedQueueWorker.stop_all(timeout=timeout)
    AsyncRunWorker.shutdown_all()
    if not stopped:
        pytest.fail(
            f"an embedded queue worker did not stop within {timeout:g}s in teardown;"
            " it would leak into later tests",
            pytrace=False,
        )
