"""ARCH-5: raw script needs exec; command criteria stay on the runs scope (D2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from recertia.app.runs import RunService
from recertia.errors import Forbidden
from recertia.ports.identity import Principal


@pytest.fixture(autouse=True)
def _execution_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("recertia.app.runs.ensure_api_execution_ready", lambda: "local")


class _Quota:
    def admit(self, tenant_id: str, *, quota: object = None) -> None:
        del tenant_id, quota

    def release_inflight(self, tenant_id: str) -> None:
        del tenant_id

    def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
        del tenant_id, cost_usd


class _RecordingPdp:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def require(self, principal: Principal, action: str, resource: Any) -> None:
        del principal, resource
        self.actions.append(action)
        if action == "runs.script":
            raise Forbidden("script requires the 'exec' scope (or admin)")


def _runs_only() -> Principal:
    return Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset({"runs"})
    )


def test_script_without_exec_is_forbidden() -> None:
    pdp = _RecordingPdp()
    service = RunService(_Quota(), {}, lambda tenant, run_id: None, pdp=pdp)
    service.bind(SimpleNamespace())
    with pytest.raises(Forbidden, match="exec"):
        service.create(_runs_only(), SimpleNamespace(script=["true"]))
    assert pdp.actions == ["runs.create", "runs.script"]


def test_command_criterion_allowed_for_runs_scope_on_local_and_container() -> None:
    """D2: command criteria do not require runs.script, on either backend."""

    class _Stop(Exception):
        pass

    class _Pdp:
        def __init__(self) -> None:
            self.actions: list[str] = []

        def require(self, principal: Principal, action: str, resource: Any) -> None:
            del principal, resource
            self.actions.append(action)
            if action == "runs.script":
                raise Forbidden("script requires the 'exec' scope (or admin)")
            raise _Stop()

    for _backend in ("local", "container"):
        pdp = _Pdp()
        service = RunService(_Quota(), {}, lambda tenant, run_id: None, pdp=pdp)
        service.bind(SimpleNamespace())
        with pytest.raises(_Stop):
            service.create(_runs_only(), SimpleNamespace(script=None))
        assert pdp.actions == ["runs.create"]
