"""Command criteria go through CriterionRunner and origin=criterion."""

from __future__ import annotations

from pathlib import Path

from contracts.criteria import TaskCriterion
from recertia.ports.execution import CommandRequest, CommandResult
from recertia.review.shadow import evaluate_shadow_offline
from recertia.validation.criteria import CriterionRunner
from recertia.validation.sensitivity import _default_runner


class _RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return CommandResult(returncode=0, stdout="ok", stderr="")


def test_command_criterion_uses_executor_origin_criterion(tmp_path: Path) -> None:
    executor = _RecordingExecutor()
    criterion = TaskCriterion(id="c1", kind="command", run="echo hi", source="caller")
    result = CriterionRunner(executor).score(criterion, workdir=tmp_path)  # type: ignore[arg-type]
    assert result.passed
    assert result.exit_code == 0
    assert len(executor.requests) == 1
    assert executor.requests[0].origin == "criterion"
    assert executor.requests[0].command == "echo hi"
    assert executor.requests[0].workdir == tmp_path


def test_shadow_and_sensitivity_share_criterion_runner(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    real = CriterionRunner.score

    def spy(self, criterion, *, workdir, judge=None):  # type: ignore[no-untyped-def]
        calls.append(criterion.id)
        return real(self, criterion, workdir=workdir, judge=judge)

    monkeypatch.setattr(CriterionRunner, "score", spy)
    command = TaskCriterion(id="sens", kind="command", run="true", source="caller")
    assert _default_runner(command, tmp_path) is True

    class _Version:
        certification_criteria = [
            TaskCriterion(id="shadow-cmd", kind="command", run="true", source="caller")
        ]

    class _Store:
        def get_version(self, skill_id: str, version: int) -> _Version:
            del skill_id, version
            return _Version()

    class _Slot:
        skill_id = "s"
        version = 1

    assert evaluate_shadow_offline(_Store(), _Slot(), workdir=tmp_path) is True  # type: ignore[arg-type]
    assert calls == ["sens", "shadow-cmd"]
