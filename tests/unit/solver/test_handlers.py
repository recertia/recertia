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


def test_confined_path_refuses_escape(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="escapes workspace"):
        confined_path(tmp_path, "../outside")
    inner = confined_path(tmp_path, "ok.txt")
    assert inner.parent == tmp_path.resolve()
