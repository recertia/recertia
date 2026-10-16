"""Sandbox limits and the disabled legacy local-process executor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxLimits:
    max_cpu_seconds: int = 60
    max_address_space_mb: int = 512
    scrub_env: bool = True
    allowed_env_keys: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TMPDIR")
    allow_network: bool = False  # informational for container backends

    @classmethod
    def from_policy(cls, policy: object | None = None) -> SandboxLimits:
        """Build limits from a ``SandboxPolicy``, or ``DEFAULT_SANDBOX`` when omitted."""

        if policy is None:
            from recertia.governance.sandbox import DEFAULT_SANDBOX

            policy = DEFAULT_SANDBOX
        to_limits = getattr(policy, "to_limits", None)
        if callable(to_limits):
            return to_limits()  # type: ignore[no-any-return]
        return cls(
            max_cpu_seconds=int(getattr(policy, "max_cpu_seconds", 60)),
            max_address_space_mb=int(getattr(policy, "max_address_space_mb", 512)),
            scrub_env=bool(getattr(policy, "scrub_env", True)),
            allowed_env_keys=tuple(
                getattr(policy, "allowed_env_keys", ("PATH", "LANG", "LC_ALL", "TMPDIR"))
            ),
            allow_network=bool(getattr(policy, "allow_network", False)),
        )


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
