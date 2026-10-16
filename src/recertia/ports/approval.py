"""Approval port. Tool runtimes depend on this protocol, not on governance."""

from __future__ import annotations

from typing import Protocol


class ApprovalPolicy(Protocol):
    """Grant check used by ``ToolRuntime`` before a non-read tool runs."""

    def is_approved(self, tool: str, step_id: str) -> bool: ...
