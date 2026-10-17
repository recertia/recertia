"""Injected golden factory replaces a direct graph-engine import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recertia.graph.engine import GraphOrchestrator


class _State:
    terminal = "solved"
    mea_active = False


class _Orchestrator:
    def start(self, *args: object, **kwargs: object) -> _State:
        del args, kwargs
        return _State()

    def close(self) -> None:
        return None


class _Factory:
    executor = None

    def __init__(self) -> None:
        self.built: list[Path] = []
        self.policies: list[object] = []

    def build(self, runs_root: Path, *, policy: object | None = None) -> _Orchestrator:
        self.built.append(Path(runs_root))
        self.policies.append(policy)
        return _Orchestrator()


def _goal_dir(tmp_path: Path) -> Path:
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "goal.json").write_text(
        json.dumps(
            {"desired": [{"id": "f", "kind": "file_exists", "path": "marker.txt", "weight": 1.0}]}
        ),
        encoding="utf-8",
    )
    (golden / "expect.json").write_text(json.dumps({"terminal": "solved"}), encoding="utf-8")
    return golden


def test_goal_fixture_uses_injected_factory_not_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from recertia.evals.golden import run_goal_fixture

    def boom(self: object, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise AssertionError("golden constructed GraphOrchestrator")

    monkeypatch.setattr(GraphOrchestrator, "__init__", boom)
    factory = _Factory()
    result = run_goal_fixture(
        _goal_dir(tmp_path),
        runs_root=tmp_path / "runs",
        factory=factory,
    )
    assert result.passed
    assert result.terminal == "solved"
    assert factory.built
    assert factory.built[0].name == "golden-runs"


def test_review_service_uses_injected_gate(tmp_path: Path) -> None:
    from recertia.review import ReviewService

    seen: dict[str, object] = {}

    class _Report:
        results = [object()]
        all_passed = False

        def write(self, path: Path) -> None:
            Path(path).write_text("{}\n", encoding="utf-8")

    class _Gate:
        def run(self, version: object, **kwargs: object) -> _Report:
            seen["version"] = version
            seen["kwargs"] = kwargs
            return _Report()

    reviewer = ReviewService(
        tmp_path / "review",
        golden_root=tmp_path / "goldens",
        runs_root=tmp_path / "runs",
        gate=_Gate(),
    )
    version = _version()
    decision = reviewer.decide(version, run_id="run-1")
    assert decision.outcome == "rejected"
    assert seen["version"] is version
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["golden_root"] == tmp_path / "goldens"
    assert kwargs["runs_root"] == tmp_path / "runs"


def _version() -> object:
    from datetime import datetime, timezone

    from contracts.criteria import SensitivityProof, SkillCertificationCriterion
    from contracts.skill import Hygiene, Provenance, SkillVersion, Step

    proof = SensitivityProof(
        criterion_id="ok",
        negative_fixture="empty",
        rejected=True,
        checked_at=datetime.now(timezone.utc),
        evidence_hash="abc",
    )
    return SkillVersion(
        skill_id="gate-demo",
        version=1,
        title="Title for gate demo skill",
        intent="Intent text long enough for the gate demo skill version.",
        task_class="repo-chore",
        steps=[
            Step(
                id="step_1",
                tool="shell",
                intent="Run a trivial shell step for the gate fixture",
                inputs={"command": "true"},
            )
        ],
        certification_criteria=[
            SkillCertificationCriterion(
                id="ok",
                kind="command",
                run="true",
                sensitivity_proof=proof,
                preregistered=True,
            )
        ],
        provenance=Provenance(
            distilled_from_run="review",
            distilled_at=datetime.now(timezone.utc),
            curation="human_authored",
            authoring_prior_version="ap-test",
        ),
        hygiene=Hygiene(secret_scan="passed", scanned_at=datetime.now(timezone.utc)),
    )
