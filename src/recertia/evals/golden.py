"""Minimal golden-regression runner (M1; specs §8 regression gate, refactor-plan B6).

One golden task per seed skill, run before that skill's ``SkillStatus.lifecycle`` is set to
``approved``. The full harness (fixtures per task class, snapshot pinning, ``causal_lift``)
is M4; this is the narrow slice the seed library needs so "approving the seed library" is
not a documented exception to a rule that does not exist yet.

A golden task is a directory::

    evals/golden/<task_class>/<skill_id>/
        goal.json          # preferred (Variant B Goal)
        task.json          # {request, expected_skill_id, expected_version?, criteria?}
        workspace/         # fixture files copied into the run workdir
        expect.json        # {terminal: "solved"} (M1 minimal)

When ``goal.json`` is present it is used as the primary input; ``task.json`` request remains
the legacy fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.goal import Goal, compile_goal
from contracts.policy import ImprovementFlags, Policy
from contracts.run import RunManifest, Task
from contracts.skill import SkillVersion
from recertia.graph.engine import GraphOrchestrator
from recertia.memory.procedural.apply import script_from_skill
from recertia.memory.procedural.store import SkillStore

if TYPE_CHECKING:
    from recertia.evals.store import EvalStore


@dataclass
class GoldenResult:
    skill_id: str
    version: int
    golden_path: str
    passed: bool
    terminal: str | None
    run_id: str
    detail: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GoldenReport:
    results: list[GoldenResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "all_passed": self.all_passed,
            "results": [
                {
                    "skill_id": r.skill_id,
                    "version": r.version,
                    "golden_path": r.golden_path,
                    "passed": r.passed,
                    "terminal": r.terminal,
                    "run_id": r.run_id,
                    "detail": r.detail,
                    "at": r.at.isoformat(),
                }
                for r in self.results
            ],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def discover_golden(golden_root: Path, skill_id: str, task_class: str = "repo-chore") -> Path | None:
    path = golden_root / task_class / skill_id
    has_task = path.is_dir() and (path / "task.json").exists()
    has_goal = path.is_dir() and (path / "goal.json").exists()
    return path if has_task or has_goal else None


def list_goldens_for_task_class(golden_root: Path, task_class: str = "repo-chore") -> list[Path]:
    """Golden fixture directories for a task class, sorted by name."""

    root = golden_root / task_class
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "goal.json").exists() or (child / "task.json").exists():
            found.append(child)
    return found


def discover_version_golden(
    golden_root: Path, skill_id: str, version: int, task_class: str = "repo-chore"
) -> Path | None:
    """Optional per-version fixture dir: ``<root>/<task_class>/<skill_id>/v<N>/``."""

    path = golden_root / task_class / skill_id / f"v{version}"
    has_task = path.is_dir() and (path / "task.json").exists()
    has_goal = path.is_dir() and (path / "goal.json").exists()
    return path if has_task or has_goal else None


def run_golden_for_skill(
    version: SkillVersion,
    golden_dir: Path,
    *,
    runs_root: Path,
    use_skill_script: bool = True,
    snapshot_id: str | None = None,
    model_version: str | None = None,
    eval_store: EvalStore | None = None,
) -> GoldenResult:
    """Execute one golden task against ``version``; return a :class:`GoldenResult`."""

    task_spec: dict = {}
    if (golden_dir / "task.json").exists():
        task_spec = json.loads((golden_dir / "task.json").read_text(encoding="utf-8"))

    goal: Goal | None = None
    if (golden_dir / "goal.json").exists():
        goal = Goal.model_validate_json((golden_dir / "goal.json").read_text(encoding="utf-8"))

    expect = {}
    expect_path = golden_dir / "expect.json"
    if expect_path.exists():
        expect = json.loads(expect_path.read_text(encoding="utf-8"))
    expected_terminal = expect.get("terminal", "solved")

    workdir = (
        runs_root
        / "golden-workspaces"
        / f"{version.skill_id}-v{version.version}-{uuid.uuid4().hex[:8]}"
    )
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = golden_dir / "workspace"
    if fixture.exists():
        for item in fixture.iterdir():
            dest = workdir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    try:
        if goal is not None and "criteria" not in task_spec:
            criteria = compile_goal(goal, source="caller")
        else:
            criteria = _criteria_from_task(task_spec, version)
    except ValueError as exc:
        return GoldenResult(
            skill_id=version.skill_id,
            version=version.version,
            golden_path=str(golden_dir),
            passed=False,
            terminal=None,
            run_id="",
            detail=str(exc),
        )
    script = script_from_skill(version) if use_skill_script else task_spec.get("script", ["true"])

    run_id = f"golden-{version.skill_id}-v{version.version}-{uuid.uuid4().hex[:6]}"
    from contracts.run import RunManifest

    pinned = RunManifest(
        model_version=model_version or "m4-harness",
        index_snapshot_id=snapshot_id,
        library_commit=snapshot_id,
    )
    request = task_spec.get("request") or (goal.context if goal else None)
    orch = GraphOrchestrator(runs_root / "golden-runs")
    previous_backend = os.environ.get("RECERTIA_EXECUTION_BACKEND")
    if previous_backend is None:
        os.environ["RECERTIA_EXECUTION_BACKEND"] = "local"
    try:
        state = orch.start(
            run_id,
            Task(
                task_id=run_id,
                goal=goal,
                request=request,
                task_class=version.task_class,
                submitted_at=datetime.now(timezone.utc),
                is_eval_fixture=True,
            ),
            criteria,
            budget=Budget(max_attempts=2),
            workdir=workdir,
            script=script,
            manifest=pinned,
            arm="treatment",
        )
    finally:
        orch.close()
        if previous_backend is None:
            os.environ.pop("RECERTIA_EXECUTION_BACKEND", None)
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)

    if eval_store is not None:
        from recertia.evals.store import ObservationError

        try:
            eval_store.append_run(state)
        except ObservationError:
            pass

    passed = state.terminal == expected_terminal
    return GoldenResult(
        skill_id=version.skill_id,
        version=version.version,
        golden_path=str(golden_dir),
        passed=passed,
        terminal=state.terminal,
        run_id=run_id,
        detail=(
            f"expected terminal={expected_terminal!r}, got {state.terminal!r}; "
            f"snapshot={pinned.index_snapshot_id!r} model={pinned.model_version!r}"
        ),
    )


def _policy_for_mea_fixture(goal: Goal | None, task_spec: dict) -> Policy | None:
    """Supply the policy layer only when the fixture requested MEA.

    The other two layers stay on the Goal / Task. Default goldens are unchanged.
    """

    strategy = task_spec.get("execution_strategy", "single")
    opt_in = bool(goal is not None and goal.mea_opt_in)
    if strategy != "mea" and not opt_in:
        return None
    return Policy(
        version="mea-golden",
        authoring_prior_version="mea-golden",
        improvement=ImprovementFlags(mea_enabled=True),
    )


def run_goal_fixture(
    golden_dir: Path,
    *,
    runs_root: Path,
    eval_store: "EvalStore | None" = None,
    snapshot_id: str | None = None,
    model_version: str | None = None,
) -> GoldenResult:
    """Run a skill-free Goal golden (MEA multiphase and similar).

    Not a promotion-gate fixture: ``run_task_class_gate`` is skill-scoped and
    must not pick these up. Eval observations are recorded with
    ``is_eval_fixture=True`` so they cannot enter causal_lift samples.
    """

    task_spec: dict = {}
    if (golden_dir / "task.json").exists():
        task_spec = json.loads((golden_dir / "task.json").read_text(encoding="utf-8"))

    goal: Goal | None = None
    if (golden_dir / "goal.json").exists():
        goal = Goal.model_validate_json((golden_dir / "goal.json").read_text(encoding="utf-8"))

    expect = {}
    expect_path = golden_dir / "expect.json"
    if expect_path.exists():
        expect = json.loads(expect_path.read_text(encoding="utf-8"))
    expected_terminal = expect.get("terminal", "solved")

    workdir = (
        runs_root
        / "golden-workspaces"
        / f"{golden_dir.name}-{uuid.uuid4().hex[:8]}"
    )
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = golden_dir / "workspace"
    if fixture.exists():
        for item in fixture.iterdir():
            dest = workdir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    label = golden_dir.name
    try:
        if goal is None:
            raise ValueError("skill-free golden requires goal.json")
        criteria = compile_goal(goal, source="caller")
    except ValueError as exc:
        return GoldenResult(
            skill_id=label,
            version=0,
            golden_path=str(golden_dir),
            passed=False,
            terminal=None,
            run_id="",
            detail=str(exc),
        )

    script = task_spec.get("script") or ["true"]
    if isinstance(script, str):
        script = [script]
    strategy = task_spec.get("execution_strategy", "single")
    if strategy not in ("single", "mea"):
        strategy = "single"
    run_id = f"golden-{label}-{uuid.uuid4().hex[:6]}"
    pinned = RunManifest(
        model_version=model_version or "m4-harness",
        index_snapshot_id=snapshot_id or "mea-golden",
        library_commit=snapshot_id or "mea-golden",
    )
    request = task_spec.get("request") or goal.context
    orch = GraphOrchestrator(
        runs_root / "golden-runs",
        policy=_policy_for_mea_fixture(goal, task_spec),
    )
    previous_backend = os.environ.get("RECERTIA_EXECUTION_BACKEND")
    if previous_backend is None:
        os.environ["RECERTIA_EXECUTION_BACKEND"] = "local"
    try:
        state = orch.start(
            run_id,
            Task(
                task_id=run_id,
                goal=goal,
                request=request,
                task_class=goal.task_class or task_spec.get("task_class") or label,
                submitted_at=datetime.now(timezone.utc),
                is_eval_fixture=True,
                execution_strategy="mea" if strategy == "mea" else "single",
            ),
            criteria,
            budget=Budget(max_attempts=2),
            workdir=workdir,
            script=script,
            manifest=pinned,
            arm="treatment",
        )
    finally:
        orch.close()
        if previous_backend is None:
            os.environ.pop("RECERTIA_EXECUTION_BACKEND", None)
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)

    if eval_store is not None:
        from recertia.evals.store import ObservationError

        try:
            eval_store.append_run(state)
        except ObservationError:
            pass

    passed = state.terminal == expected_terminal
    return GoldenResult(
        skill_id=label,
        version=0,
        golden_path=str(golden_dir),
        passed=passed,
        terminal=state.terminal,
        run_id=run_id,
        detail=(
            f"expected terminal={expected_terminal!r}, got {state.terminal!r}; "
            f"mea_active={state.mea_active}"
        ),
    )


def run_task_class_gate(
    version: SkillVersion,
    golden_root: Path,
    *,
    runs_root: Path,
    task_class: str | None = None,
) -> GoldenReport:
    task_class = task_class or version.task_class
    report = GoldenReport()
    class_root = golden_root / task_class
    if not class_root.is_dir():
        return report
    for child in sorted(p for p in class_root.iterdir() if p.is_dir()):
        if child.name.startswith("_"):
            continue
        if not ((child / "task.json").exists() or (child / "goal.json").exists()):
            continue
        report.results.append(
            run_golden_for_skill(version, child, runs_root=runs_root, use_skill_script=True)
        )
    return report


def select_and_run_gate(
    version: SkillVersion,
    *,
    runs_root: Path,
    golden_root: Path | None = None,
    golden_dir: Path | None = None,
    require_task_class_gate: bool = False,
    require_fixture: bool = False,
    extra_golden_dirs: list[Path] | None = None,
) -> GoldenReport:
    prefer_task_class = require_task_class_gate or (
        require_fixture and golden_root is not None and golden_dir is None
    )
    if prefer_task_class:
        if golden_root is None:
            raise ValueError("task-class regression gate requires golden_root")
        report = run_task_class_gate(
            version, golden_root, runs_root=runs_root, task_class=version.task_class
        )
        if golden_dir is not None and golden_dir.is_dir():
            own = run_golden_for_skill(version, golden_dir, runs_root=runs_root)
            if not any(r.golden_path == own.golden_path for r in report.results):
                report.results.append(own)
        return _append_extra_golden_dirs(
            version, report, extra_golden_dirs or [], runs_root=runs_root
        )

    if golden_dir is not None:
        result = run_golden_for_skill(version, golden_dir, runs_root=runs_root)
        return _append_extra_golden_dirs(
            version,
            GoldenReport(results=[result]),
            extra_golden_dirs or [],
            runs_root=runs_root,
        )

    if golden_root is not None:
        skill_dir = golden_root / version.task_class / version.skill_id
        if skill_dir.is_dir() and (
            (skill_dir / "task.json").exists() or (skill_dir / "goal.json").exists()
        ):
            report = GoldenReport(
                results=[run_golden_for_skill(version, skill_dir, runs_root=runs_root)]
            )
            return _append_extra_golden_dirs(
                version, report, extra_golden_dirs or [], runs_root=runs_root
            )
        if (golden_root / version.task_class / ".full_class").exists():
            report = run_task_class_gate(
                version,
                golden_root,
                runs_root=runs_root,
                task_class=version.task_class,
            )
            return _append_extra_golden_dirs(
                version, report, extra_golden_dirs or [], runs_root=runs_root
            )
        return _append_extra_golden_dirs(
            version, GoldenReport(), extra_golden_dirs or [], runs_root=runs_root
        )

    if require_fixture and not extra_golden_dirs:
        raise ValueError("promote_to_approved requires golden_dir or golden_root")
    return _append_extra_golden_dirs(
        version, GoldenReport(), extra_golden_dirs or [], runs_root=runs_root
    )


def _append_extra_golden_dirs(
    version: SkillVersion,
    report: GoldenReport,
    extra_golden_dirs: list[Path],
    *,
    runs_root: Path,
) -> GoldenReport:
    """Run predecessor (or other) fixtures not already in ``report``."""

    seen: set[str] = set()
    for result in report.results:
        if result.golden_path:
            try:
                seen.add(str(Path(result.golden_path).resolve()))
            except OSError:
                seen.add(result.golden_path)
    for extra in extra_golden_dirs:
        try:
            key = str(extra.resolve()) if extra.exists() else str(extra)
        except OSError:
            key = str(extra)
        if key in seen:
            continue
        seen.add(key)
        if not extra.is_dir() or not (
            (extra / "task.json").exists() or (extra / "goal.json").exists()
        ):
            report.results.append(
                GoldenResult(
                    skill_id=version.skill_id,
                    version=version.version,
                    golden_path=str(extra),
                    passed=False,
                    terminal=None,
                    run_id="",
                    detail="predecessor golden fixture missing or unreadable",
                )
            )
            continue
        report.results.append(run_golden_for_skill(version, extra, runs_root=runs_root))
    return report


def run_seed_library_gate(
    store: SkillStore,
    golden_root: Path,
    *,
    runs_root: Path,
    log_path: Path,
    skill_ids: list[str] | None = None,
) -> GoldenReport:
    report = GoldenReport()
    for version, _status, _stats in store.iter_loaded():
        if skill_ids is not None and version.skill_id not in skill_ids:
            continue
        golden = discover_golden(golden_root, version.skill_id, version.task_class)
        if golden is None:
            report.results.append(
                GoldenResult(
                    skill_id=version.skill_id,
                    version=version.version,
                    golden_path="",
                    passed=False,
                    terminal=None,
                    run_id="",
                    detail=f"no golden task under {golden_root}/{version.task_class}/{version.skill_id}",
                )
            )
            continue
        report.results.append(
            run_golden_for_skill(version, golden, runs_root=runs_root)
        )
    report.write(log_path)
    return report


def _criteria_from_task(task_spec: dict, version: SkillVersion) -> list[TaskCriterion]:
    if "criteria" in task_spec:
        out = [TaskCriterion(**c) for c in task_spec["criteria"]]
        proven = [
            c
            for c in out
            if c.is_required and c.kind != "judge" and c.is_preregistered_and_proven
        ]
        if not proven:
            raise ValueError(
                f"golden task cannot promote {version.skill_id}@v{version.version}: "
                "task criteria lack a required non-judge criterion with hashed rejecting "
                "sensitivity evidence"
            )
        return out
    adapted: list[TaskCriterion] = []
    for c in version.certification_criteria:
        if c.kind == "judge" or not c.is_required:
            continue
        if not c.is_preregistered_and_proven:
            raise ValueError(
                f"golden task cannot promote {version.skill_id}@v{version.version}: "
                f"criterion {c.id!r} lacks hashed rejecting sensitivity evidence"
            )
        proof = c.sensitivity_proof
        adapted.append(
            TaskCriterion(
                id=c.id,
                kind=c.kind,  # type: ignore[arg-type]
                run=c.run,
                expect_exit=c.expect_exit,
                source="task_class_template",
                weight=c.weight,
                sensitivity_proof=proof,
            )
        )
    if not adapted:
        raise ValueError(
            f"golden task cannot promote {version.skill_id}@v{version.version}: "
            "no required non-judge criterion with hashed sensitivity evidence"
        )
    if not any(c.is_preregistered_and_proven for c in adapted):
        raise ValueError(
            f"golden task cannot promote {version.skill_id}@v{version.version}: "
            "adapted task criteria failed sensitivity evidence verification"
        )
    return adapted


def run_eval_suite(
    *,
    task_class: str,
    golden_root: Path,
    skills_root: Path,
    runs_root: Path,
    eval_store: EvalStore,
    snapshot_id: str,
    model_version: str | None = None,
    golden_dir: Path | None = None,
) -> GoldenReport:
    """Run golden fixtures as eval (firewall on) and append observations."""

    store = SkillStore(skills_root)
    report = GoldenReport()
    if golden_dir is not None:
        skill_id = golden_dir.name
        loaded = [
            v
            for v, _st, _stats in store.iter_loaded()
            if v.skill_id == skill_id and v.task_class == task_class
        ]
        if not loaded:
            from recertia.memory.procedural.seeds import SEED_SKILLS

            loaded = [v for v in SEED_SKILLS if v.skill_id == skill_id]
        if not loaded:
            if (golden_dir / "goal.json").exists() or (golden_dir / "task.json").exists():
                report.results.append(
                    run_goal_fixture(
                        golden_dir,
                        runs_root=runs_root,
                        eval_store=eval_store,
                        snapshot_id=snapshot_id,
                        model_version=model_version,
                    )
                )
                return report
            report.results.append(
                GoldenResult(
                    skill_id=skill_id,
                    version=1,
                    golden_path=str(golden_dir),
                    passed=False,
                    terminal=None,
                    run_id="",
                    detail="no skill version available for eval fixture",
                )
            )
            return report
        report.results.append(
            run_golden_for_skill(
                loaded[0],
                golden_dir,
                runs_root=runs_root,
                snapshot_id=snapshot_id,
                model_version=model_version,
                eval_store=eval_store,
            )
        )
        return report

    matched = False
    for version, _status, _stats in store.iter_loaded():
        if version.task_class != task_class:
            continue
        matched = True
        golden = discover_golden(golden_root, version.skill_id, task_class)
        if golden is None:
            continue
        report.results.append(
            run_golden_for_skill(
                version,
                golden,
                runs_root=runs_root,
                snapshot_id=snapshot_id,
                model_version=model_version,
                eval_store=eval_store,
            )
        )
    if not matched:
        for gdir in list_goldens_for_task_class(golden_root, task_class):
            report.results.append(
                run_goal_fixture(
                    gdir,
                    runs_root=runs_root,
                    eval_store=eval_store,
                    snapshot_id=snapshot_id,
                    model_version=model_version,
                )
            )
    return report

