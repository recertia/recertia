"""T3 sandbox policy (ADR-0005). Mutable only via human review — never by runs/jobs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from recertia.solver.sandbox import SandboxLimits


@dataclass(frozen=True)
class SandboxPolicy:
    """Process-isolation and approval thresholds for non-read tools."""

    require_approval_for_non_read: bool = True
    allow_network: bool = False
    max_cpu_seconds: int = 60
    max_address_space_mb: int = 512
    scrub_env: bool = True
    allowed_env_keys: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TMPDIR")
    # Production commands run only in Docker or Podman; no host-process fallback.
    backend: str = "container"
    image: str | None = "python:3.12-slim"

    def to_limits(self) -> SandboxLimits:
        """Map this policy onto the executor's ``SandboxLimits``."""

        from recertia.solver.sandbox import SandboxLimits

        return SandboxLimits(
            max_cpu_seconds=self.max_cpu_seconds,
            max_address_space_mb=self.max_address_space_mb,
            scrub_env=self.scrub_env,
            allowed_env_keys=self.allowed_env_keys,
            allow_network=self.allow_network,
        )


DEFAULT_SANDBOX = SandboxPolicy()


@dataclass
class ApprovalRecord:
    tool: str
    step_id: str
    actor: str
    reason: str = ""


@dataclass
class ApprovalGate:
    """Allowlist of approved (tool, step) or tool-wide grants for one run."""

    _grants: set[tuple[str, str | None]] = field(default_factory=set)
    _records: list[ApprovalRecord] = field(default_factory=list)

    def approve(
        self,
        tool: str,
        *,
        step_id: str | None = None,
        actor: str = "operator",
        reason: str = "",
    ) -> None:
        self._grants.add((tool, step_id))
        self._records.append(
            ApprovalRecord(tool=tool, step_id=step_id or "*", actor=actor, reason=reason)
        )

    def is_approved(self, tool: str, step_id: str) -> bool:
        return (tool, step_id) in self._grants or (tool, None) in self._grants

    @property
    def grants(self) -> frozenset[tuple[str, str | None]]:
        """Read-only view of approved (tool, step_id) pairs."""

        return frozenset(self._grants)

    @property
    def records(self) -> Sequence[ApprovalRecord]:
        """Read-only view of approval audit records."""

        return tuple(self._records)
