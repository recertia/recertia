"""Tool invocation runtime with claim scheduling and approval gates (specs §26.2, M2)."""

from __future__ import annotations

import contextvars
import inspect
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from contracts.resources import ResourceClaim
from recertia.ports.approval import ApprovalPolicy as ApprovalGate
from recertia.ports.execution import CommandOrigin
from recertia.solver.claims import ClaimScheduler
from recertia.solver.registry import ToolRegistry, ToolResult
from recertia.solver.sandbox import SandboxLimits

if TYPE_CHECKING:
    from recertia.ports.execution import CommandExecutor
    from recertia.solver.model import ModelClient
    from recertia.solver.result_cache import ToolResultCache

_ACTIVE_SANDBOX_LIMITS: contextvars.ContextVar[SandboxLimits | None] = contextvars.ContextVar(
    "recertia_active_sandbox_limits", default=None
)
_ACTIVE_MODEL: contextvars.ContextVar["ModelClient | None"] = contextvars.ContextVar(
    "recertia_active_model", default=None
)
_ACTIVE_STEP_CONTEXT: contextvars.ContextVar["StepInvokeContext | None"] = contextvars.ContextVar(
    "recertia_active_step_context", default=None
)


@dataclass(frozen=True)
class StepInvokeContext:
    """Skill-step metadata available to handlers without polluting tool inputs."""

    intent: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class HandlerContext:
    """Explicit invoke context. ContextVar shims stay for unmigrated handlers."""

    workdir: Path
    executor: "CommandExecutor | None" = None
    limits: SandboxLimits | None = None
    model: "ModelClient | None" = None
    step: StepInvokeContext | None = None
    run_id: str | None = None
    owner_id: str | None = None
    origin: CommandOrigin = "tool"


def _accepts_handler_context(handler: Callable[..., object]) -> bool:
    """True when the handler's second parameter is annotated as HandlerContext."""

    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return False
    if len(params) < 2:
        return False
    ann = params[1].annotation
    if ann is HandlerContext:
        return True
    name = getattr(ann, "__name__", None)
    text = name if isinstance(name, str) else str(ann)
    return "HandlerContext" in text


def active_sandbox_limits() -> SandboxLimits:
    """Limits for the in-flight ``ToolRuntime.invoke`` (handlers may read this)."""

    return _ACTIVE_SANDBOX_LIMITS.get() or SandboxLimits()


def active_model() -> "ModelClient | None":
    """Model client for the in-flight invoke (``agent_subtask`` / model-backed tools)."""

    return _ACTIVE_MODEL.get()


def active_step_context() -> StepInvokeContext:
    """Intent / bound params for the in-flight skill step (empty outside applicator)."""

    return _ACTIVE_STEP_CONTEXT.get() or StepInvokeContext()


class ApprovalRequiredError(PermissionError):
    """Raised when a non-read tool is invoked without an approval grant."""


class ToolRuntime:
    """Invokes registered tools; records affordance-relevant outcomes."""

    def __init__(
        self,
        registry: ToolRegistry,
        scheduler: ClaimScheduler | None = None,
        *,
        require_approval_for_non_read: bool = True,
        approval_gate: ApprovalGate | None = None,
        sandbox_limits: SandboxLimits | None = None,
        sandbox_policy: object | None = None,
        model: "ModelClient | None" = None,
        result_cache: "ToolResultCache | None" = None,
    ) -> None:
        self._registry = registry
        self.scheduler = scheduler or ClaimScheduler()
        self._invocations: list[ToolResult] = []
        self.require_approval_for_non_read = require_approval_for_non_read
        self.approval_gate = approval_gate
        self.model = model
        self.result_cache = result_cache
        to_limits = getattr(sandbox_policy, "to_limits", None) if sandbox_policy is not None else None
        if sandbox_limits is not None:
            self.sandbox_limits = sandbox_limits
        elif callable(to_limits):
            self.sandbox_limits = to_limits()
        else:
            self.sandbox_limits = SandboxLimits()

    @property
    def invocations(self) -> Sequence[ToolResult]:
        """Read-only view of tool results recorded for this runtime."""

        return tuple(self._invocations)

    def invoke(
        self,
        tool_name: str,
        inputs: dict,
        *,
        workdir: Path,
        step_id: str,
        extra_claims: list[ResourceClaim] | None = None,
        step_context: StepInvokeContext | None = None,
        origin: CommandOrigin = "tool",
    ) -> ToolResult:
        tool = self._registry.get(tool_name)
        if (
            self.require_approval_for_non_read
            and tool.side_effect not in ("read", "pure")
        ):
            gate = self.approval_gate
            if gate is None or not gate.is_approved(tool_name, step_id):
                raise ApprovalRequiredError(
                    f"tool {tool_name!r} side_effect={tool.side_effect!r} requires approval"
                )
        claims = list(tool.claims) + list(extra_claims or [])
        self.scheduler.acquire(step_id, claims)
        started = time.monotonic()
        limits_token = _ACTIVE_SANDBOX_LIMITS.set(self.sandbox_limits)
        model_token = _ACTIVE_MODEL.set(self.model)
        step_token = _ACTIVE_STEP_CONTEXT.set(step_context or StepInvokeContext())
        snapshot_hash = ""
        try:
            from recertia.observability.systems import canonical_tool_key, snapshot_stat_hash
            from recertia.telemetry import emit_in_run

            snapshot_hash = snapshot_stat_hash(workdir)
            cached = None
            if self.result_cache is not None:
                cached = self.result_cache.lookup(tool, inputs, snapshot_hash=snapshot_hash)
            if cached is not None:
                cached.duration_s = time.monotonic() - started
                cached.claimed = claims
                self._invocations.append(cached)
                emit_in_run(
                    "tool.invoked",
                    tool=tool_name,
                    side_effect=tool.side_effect,
                    cache="hit",
                    canonical_key=canonical_tool_key(tool_name, inputs, snapshot_hash),
                    ok=cached.ok,
                )
                emit_in_run("cache.hit", kind="tool", tool=tool_name)
                return cached
            handler = self._registry.handler(tool_name)
            if _accepts_handler_context(handler):
                result = handler(
                    inputs,
                    HandlerContext(
                        workdir=workdir,
                        limits=self.sandbox_limits,
                        model=self.model,
                        step=step_context or StepInvokeContext(),
                        origin=origin,
                    ),
                )
            else:
                result = handler(inputs, workdir)
            result.duration_s = time.monotonic() - started
            result.claimed = claims
            if not result.ok:
                sig = self._registry.match_error_signature(
                    tool_name, result.stdout + result.stderr
                )
                result.error_signature = sig
            if self.result_cache is not None:
                self.result_cache.store(tool, inputs, result, snapshot_hash=snapshot_hash)
                emit_in_run("cache.miss", kind="tool", tool=tool_name)
                if tool.side_effect not in ("read", "pure"):
                    self.result_cache.invalidate_all()
            self._invocations.append(result)
            emit_in_run(
                "tool.invoked",
                tool=tool_name,
                side_effect=tool.side_effect,
                cache="miss" if self.result_cache is not None else "off",
                canonical_key=canonical_tool_key(tool_name, inputs, snapshot_hash),
                ok=result.ok,
            )
            return result
        finally:
            _ACTIVE_STEP_CONTEXT.reset(step_token)
            _ACTIVE_MODEL.reset(model_token)
            _ACTIVE_SANDBOX_LIMITS.reset(limits_token)
            self.scheduler.release(step_id, claims)

    def is_flaky(self, tool_name: str) -> bool:
        """Read-only tool metadata needed for failure classification."""
        return self._registry.is_flaky(tool_name)

    def names(self) -> list[str]:
        """Read-only names; mutation remains private to the registry owner."""
        return self._registry.names()

    def match_error_signature(self, tool_name: str, output: str) -> str | None:
        """Read-only error metadata; does not expose registry mutation."""
        return self._registry.match_error_signature(tool_name, output)
