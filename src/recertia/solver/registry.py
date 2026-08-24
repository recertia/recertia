"""Tool catalogue: side-effect classes, handlers, default first-domain tools (specs §26.2, M2).

The registry *contents* are T3 (code review only — ADR-0005): runs invoke tools through
:class:`~recertia.solver.runtime.ToolRuntime` but never mutate the registry. Mutation APIs
live on :class:`ToolRegistry` and are not injected into ``NodeContext``.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from contracts.resources import ResourceClaim

SideEffectClass = Literal["read", "write", "network", "external", "pure"]


@dataclass(frozen=True)
class Tool:
    name: str
    side_effect: SideEffectClass
    claims: tuple[ResourceClaim, ...] = ()
    description: str = ""
    flaky: bool = False
    """When True, classify_failure treats errors from this tool as ``tool`` (not execution)."""

    error_signatures: tuple[str, ...] = ()
    """Substrings that, when present in stderr/stdout, mark a known tool failure mode."""


@dataclass
class ToolResult:
    tool: str
    ok: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    cost_usd: float = 0.0
    error_signature: str | None = None
    claimed: list[ResourceClaim] = field(default_factory=list)


Handler = Callable[[dict, Path], ToolResult]

_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
"""Files larger than this are skipped by the in-process grep tool (vendored bundles, blobs)."""

_READ_FILE_TAIL_BYTES = 64 * 1024
"""read_file returns only a tail slice; files beyond this size are read from the end."""


class ToolRegistry:
    """Process-global catalogue. Populate at startup; treat as immutable thereafter."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._handlers: dict[str, Handler] = {}

    def register(self, tool: Tool, handler: Handler) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered (T3: no silent overwrite)")
        self._tools[tool.name] = tool
        self._handlers[tool.name] = handler

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def handler(self, name: str) -> Handler:
        return self._handlers[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def is_flaky(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.flaky)

    def match_error_signature(self, name: str, output: str) -> str | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        for sig in tool.error_signatures:
            if sig in output:
                return sig
        return None


def _fetch_allowlist() -> tuple[str, ...]:
    import os

    raw = os.environ.get(
        "RECERTIA_FETCH_ALLOWLIST",
        "pypi.org,files.pythonhosted.org,raw.githubusercontent.com,api.github.com",
    )
    return tuple(host.strip().lower() for host in raw.split(",") if host.strip())


def _host_allowed(hostname: str, allowlist: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    for allowed in allowlist:
        needle = allowed.lower().rstrip(".")
        if not needle:
            continue
        if host == needle:
            return True
    return False


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused (fetch allowlist is hop-exact)", headers, fp
        )


def _https_get(url: str, *, timeout_s: float, max_bytes: int) -> bytes:
    """HTTPS GET that does not follow redirects."""

    req = urllib.request.Request(
        url,
        headers={"user-agent": "recertia-fetch/0.1", "accept": "application/json,text/*"},
        method="GET",
    )
    opener = urllib.request.build_opener(_RefuseRedirect)
    with opener.open(req, timeout=timeout_s) as resp:
        return resp.read(max_bytes + 1)


def default_registry() -> ToolRegistry:
    """First-domain tools plus the gated external_computer affordance (ADR-0019)."""

    from recertia.solver.handlers import register_first_domain_tools

    registry = ToolRegistry()
    register_first_domain_tools(registry)
    return registry
