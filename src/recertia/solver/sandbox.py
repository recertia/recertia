"""Sandbox limits and the disabled legacy local-process executor.

``SandboxLimits`` lives on the execution port. This module re-exports it so
existing imports keep working for one release.
"""

from __future__ import annotations

from pathlib import Path

from recertia.errors import ExecutionUnavailable
from recertia.ports.execution import SandboxLimits

__all__ = ["SandboxError", "SandboxLimits", "run_sandboxed"]


class SandboxError(ExecutionUnavailable):
    """Command execution failed closed. Subclasses ``RecertiaError``, not ``RuntimeError``."""


def run_sandboxed(
    command: str,
    *,
    workdir: Path,
    limits: SandboxLimits | None = None,
    timeout_s: int = 60,
) -> object:
    """Rejected legacy API kept only to give callers a clear migration error."""

    del command, workdir, limits, timeout_s
    raise SandboxError(
        "host subprocess sandbox is disabled; use recertia.solver.container.run_in_container"
    )
