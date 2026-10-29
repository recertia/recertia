"""``recertia run`` and ``recertia resume`` turn a Recertia error into its exit code and one line.

No traceback reaches the terminal: the error's ``code: message`` goes to stderr and the
command exits with the error's code (7 for ``authoring_prior_missing``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from recertia.cli import runs as cli_runs
from recertia.cli.main import app
from recertia.distill.prior import AuthoringPriorMissing

runner = CliRunner()


def _raise_missing_prior(**_kwargs: object) -> None:
    raise AuthoringPriorMissing("default authoring prior policy/authoring-prior.json not found")


@pytest.mark.parametrize("command", ["run", "resume"])
def test_a_recertia_error_exits_with_its_code_and_no_traceback(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_runs, "_prepare_execution", lambda: None)
    target = "_execute_run" if command == "run" else "_execute_resume"
    monkeypatch.setattr(cli_runs, target, _raise_missing_prior)
    goal = tmp_path / "goal.json"
    goal.write_text("{}")
    argv = ["run", "--goal", str(goal)] if command == "run" else ["resume", "r1"]
    result = runner.invoke(app, [*argv, "--runs-root", str(tmp_path / "runs")])
    assert result.exit_code == 7, result.output
    assert not isinstance(result.exception, AuthoringPriorMissing), result.exception
    assert "authoring_prior_missing: default authoring prior" in result.output, result.output
    assert "Traceback" not in result.output, result.output
