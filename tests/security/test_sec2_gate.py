"""SEC-2: raw script requires exec/admin; runs-scope commands stay allowed."""

from __future__ import annotations

import pytest

from recertia.app.authz import assert_run_executable
from recertia.errors import Forbidden
from recertia.ports.identity import Principal


def _principal(*scopes: str) -> Principal:
    return Principal.from_api_key(
        key_id="k", tenant_id="t1", scopes=frozenset(scopes)
    )


def test_runs_only_script_is_forbidden() -> None:
    with pytest.raises(Forbidden, match="exec"):
        assert_run_executable(_principal("runs"), script=True)


def test_exec_plus_script_is_ok() -> None:
    assert_run_executable(_principal("runs", "exec"), script=True)


def test_admin_plus_script_is_ok() -> None:
    assert_run_executable(_principal("admin"), script=True)


def test_runs_only_without_script_is_ok() -> None:
    assert_run_executable(_principal("runs"), script=False)
