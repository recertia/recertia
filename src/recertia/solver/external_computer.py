"""Optional external-computer tool (ADR-0019). Never the default backend.

The tool is registered so plan/retrieve can see the affordance. The handler is a
gate: it refuses unless both policy flags are on *and* the backend is allow-listed.
This build does not open a Grok Bot or any other long-lived VM. Approved skill
state is never written from this path.
"""

from __future__ import annotations

from pathlib import Path

from recertia.solver.registry import ToolResult


def external_computer_handler(inputs: dict, workdir: Path) -> ToolResult:
    del workdir
    from recertia.policy_load import load_policy

    policy = load_policy()
    isolation = policy.isolation
    backend = str(inputs.get("backend") or "grok_bot")
    reasons: list[str] = []
    if not isolation.allow_external_computer:
        reasons.append("isolation.allow_external_computer is false")
    if not policy.improvement.long_lived_computer_backend:
        reasons.append("improvement.long_lived_computer_backend is false")
    allow = list(isolation.external_computer_allowlist)
    if not allow:
        reasons.append("isolation.external_computer_allowlist is empty")
    elif backend not in allow:
        reasons.append(f"backend {backend!r} is not allow-listed")
    if reasons:
        return ToolResult(
            tool="external_computer",
            ok=False,
            exit_code=2,
            stderr="; ".join(reasons) + "; default remains --rm container",
        )
    return ToolResult(
        tool="external_computer",
        ok=False,
        exit_code=3,
        stderr=(
            "external_computer is registered but no live backend is wired in this "
            "build; Recertia will not open a standing VM or write approved state"
        ),
    )
