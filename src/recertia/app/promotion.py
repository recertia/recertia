"""Promote a skill through ``GateRunner``. CLI and HTTP share this service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recertia.app.authz import Resource
from recertia.errors import Forbidden
from recertia.ports.identity import Principal
from recertia.ports.orchestration import GateRunner
from recertia.review.gate import GoldenGateRunner
from recertia.review.promotion import promote_to_approved


class PromotionService:
    """``pdp.require(skills.promote)`` then the golden gate. Routes do not run the gate."""

    def __init__(
        self,
        *,
        pdp: Any | None = None,
        gate: GateRunner | None = None,
    ) -> None:
        self._pdp = pdp
        self._gate: GateRunner = gate if gate is not None else GoldenGateRunner()

    def promote(
        self,
        principal: Principal,
        skill_id: str,
        version: int,
        *,
        store: Any,
        runs_root: Path,
        log_dir: Path,
        golden_dir: Path | None = None,
        golden_root: Path | None = None,
        model_validated_on: str = "m1-seed",
        tool_fingerprint: dict[str, str] | None = None,
        repo_root: Path | None = None,
        require_task_class_gate: bool = False,
        console_roles: frozenset[str] | None = None,
    ) -> Any:
        self._require(principal, console_roles=console_roles)
        return promote_to_approved(
            store,
            skill_id,
            version,
            golden_dir=golden_dir,
            golden_root=golden_root,
            runs_root=runs_root,
            log_dir=log_dir,
            model_validated_on=model_validated_on,
            tool_fingerprint=tool_fingerprint,
            repo_root=repo_root,
            require_task_class_gate=require_task_class_gate,
            gate=self._gate,
        )

    def _require(
        self,
        principal: Principal,
        *,
        console_roles: frozenset[str] | None,
    ) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        if console_roles is not None:
            if console_roles & {"reviewer", "admin", "platform_admin"}:
                return
            raise Forbidden("requires role reviewer")
        pdp.require(principal, "skills.promote", Resource(tenant_id=principal.tenant_id))
