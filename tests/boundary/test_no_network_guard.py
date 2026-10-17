"""Proves the repo-wide no-network guard in tests/conftest.py is live (PR5)."""

from __future__ import annotations

import os
import socket
import tempfile

import pytest

from recertia.jobs.arxiv import ArxivClient, ArxivFetchError
from recertia.solver.providers import OpenAIModelClient, ProviderError
from tests.conftest import NetworkBlockedError, network_guard_enabled

pytestmark = pytest.mark.skipif(
    not network_guard_enabled(), reason="guard opted out locally (RECERTIA_NO_NETWORK=0)"
)


def test_guard_enabled_in_ci() -> None:
    if os.environ.get("CI", "").lower() == "true":
        assert network_guard_enabled()


def test_external_connect_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkBlockedError):
            s.connect(("93.184.216.34", 443))
        with pytest.raises(NetworkBlockedError):
            s.connect_ex(("93.184.216.34", 443))


def test_external_dns_and_create_connection_blocked() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("api.openai.com", 443)
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("export.arxiv.org", 80), timeout=1)


def test_loopback_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=2):
            pass
    assert socket.getaddrinfo("localhost", 80)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_unix_socket_allowed() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(path)
            srv.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.connect(path)


def test_unmocked_provider_call_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("recertia.solver.model.time.sleep", lambda _s: None)
    with pytest.raises(ProviderError, match="blocked in tests"):
        OpenAIModelClient(api_key="k", model_id="m", max_retries=0).complete("hi")


def test_unmocked_arxiv_call_blocked() -> None:
    with pytest.raises(ArxivFetchError, match="blocked"):
        ArxivClient(min_interval_s=0).search("x")
