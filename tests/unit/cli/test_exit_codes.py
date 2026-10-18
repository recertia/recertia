"""ARCH-5 A2 CLI exits."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli.exit import exit_code_for, raise_exit
from recertia.cli.main import app
from recertia.errors import (
    Busy,
    ConfigError,
    Conflict,
    ExecutionUnavailable,
    Forbidden,
    NotFound,
    QuotaExceeded,
    Unauthorized,
)

runner = CliRunner()


def test_not_found_is_1_invalid_is_2_ledger_verify_is_2(tmp_path: Path) -> None:
    assert exit_code_for(NotFound("missing")) == 1
    assert exit_code_for(ConfigError("bad")) == 2
    assert exit_code_for(Unauthorized("no")) == 3
    assert exit_code_for(Forbidden("no")) == 3
    assert exit_code_for(QuotaExceeded("cap")) == 4
    assert exit_code_for(Conflict("exists")) == 5
    assert exit_code_for(Busy("wait")) == 6
    assert exit_code_for(ExecutionUnavailable("down")) == 7

    from typer.exceptions import Exit

    with pytest.raises(Exit) as ty:
        raise_exit(NotFound("gone"))
    assert ty.value.exit_code == 1

    with pytest.raises(Exit) as ty:
        raise_exit(ConfigError("bad"))
    assert ty.value.exit_code == 2

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("{not json\n", encoding="utf-8")
    result = runner.invoke(app, ["ledger", "verify", "--runs-root", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "LEDGER INVALID:" in (result.stderr + result.output)
