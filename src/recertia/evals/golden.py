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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.goal import Goal, compile_goal
from contracts.policy import ImprovementFlags, Policy
from contracts.run import RunManifest, Task
from contracts.skill import SkillVersion
from recertia.evals.heldout_manifest import HeldoutManifestError, verify_heldout_tree
from recertia.memory.procedural.apply import script_from_skill
from recertia.memory.procedural.store import SkillStore

if TYPE_CHECKING:
    from recertia.evals.store import EvalStore

# REC-009: held-out results never carry the fixture path, the dir name or a run id that
# embeds the skill id. This token replaces ``golden_path`` at the source.
HELDOUT_PATH_MASK = "<heldout>"

_orchestrator_factory: ContextVar[Any] = ContextVar(
    "recertia_golden_orchestrator_factory",
    default=None,
)


def _current_factory() -> Any:
    """Factory bound by the caller, or the review package's default.

    The default is loaded dynamically. A static import would chain this package
    to ``recertia.graph.engine`` through the factory, which evals-no-engine forbids.
    """

    current = _orchestrator_factory.get()
    if current is not None:
        return current
    import importlib

    gate = importlib.import_module("recertia.review.gate")
    return gate.default_golden_factory()


def _start_orchestrator(runs_root: Path, *, policy: Policy | None = None) -> Any:
    return _current_factory().build(runs_root, policy=policy)


@contextmanager
def _using_factory(factory: object | None) -> Iterator[None]:
    if factory is None:
        yield
        return
    token = _orchestrator_factory.set(factory)
    try:
        yield
    finally:
        _orchestrator_factory.reset(token)


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
    heldout: bool = False,
) -> GoldenResult:
    """Execute one golden task against ``version``; return a :class:`GoldenResult`.

    ``heldout=True`` masks at the source: ``golden_path`` is :data:`HELDOUT_PATH_MASK`,
    the run id and workdir name are random (no skill id, no dir name), and criteria
    errors are not echoed. All run state (the orchestrator's ``runtime.db`` with the
    request text and criteria commands, trajectories, the copied workspace) lives in a
    scratch dir under ``runs_root`` that is removed on every path, so only the masked
    result and the eval observation survive. Only :func:`run_heldout_suite` sets it.
    """

    if not heldout:
        return _run_golden_for_skill(
            version,
            golden_dir,
            runs_root=runs_root,
            use_skill_script=use_skill_script,
            snapshot_id=snapshot_id,
            model_version=model_version,
            eval_store=eval_store,
            heldout=False,
        )
    scratch = runs_root / f"heldout-scratch-{uuid.uuid4().hex[:12]}"
    try:
        return _run_golden_for_skill(
            version,
            golden_dir,
            runs_root=scratch,
            use_skill_script=use_skill_script,
            snapshot_id=snapshot_id,
            model_version=model_version,
            eval_store=eval_store,
            heldout=True,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_golden_for_skill(
    version: SkillVersion,
    golden_dir: Path,
    *,
    runs_root: Path,
    use_skill_script: bool,
    snapshot_id: str | None,
    model_version: str | None,
    eval_store: EvalStore | None,
    heldout: bool,
) -> GoldenResult:
    reported_path = HELDOUT_PATH_MASK if heldout else str(golden_dir)

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

    # Criteria first: a refused fixture never has its workspace copied anywhere.
    try:
        if goal is not None and "criteria" not in task_spec:
            criteria = compile_goal(goal, source="caller")
        else:
            criteria = _criteria_from_task(task_spec, version)
    except ValueError as exc:
        return GoldenResult(
            skill_id=version.skill_id,
            version=version.version,
            golden_path=reported_path,
            passed=False,
            terminal=None,
            run_id="",
            detail="held-out fixture criteria refused" if heldout else str(exc),
        )
    script = script_from_skill(version) if use_skill_script else task_spec.get("script", ["true"])

    workdir_name = (
        f"heldout-{uuid.uuid4().hex[:12]}"
        if heldout
        else f"{version.skill_id}-v{version.version}-{uuid.uuid4().hex[:8]}"
    )
    workdir = runs_root / "golden-workspaces" / workdir_name
    try:
        return _run_in_workdir(
            version,
            golden_dir,
            workdir=workdir,
            runs_root=runs_root,
            task_spec=task_spec,
            goal=goal,
            criteria=criteria,
            script=script,
            expected_terminal=expected_terminal,
            reported_path=reported_path,
            snapshot_id=snapshot_id,
            model_version=model_version,
            eval_store=eval_store,
            heldout=heldout,
        )
    finally:
        # Every path, including a copy or orchestrator failure, removes the copy.
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


def _run_in_workdir(
    version: SkillVersion,
    golden_dir: Path,
    *,
    workdir: Path,
    runs_root: Path,
    task_spec: dict,
    goal: Goal | None,
    criteria: list[TaskCriterion],
    script: Any,
    expected_terminal: str,
    reported_path: str,
    snapshot_id: str | None,
    model_version: str | None,
    eval_store: EvalStore | None,
    heldout: bool,
) -> GoldenResult:
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

    run_id = (
        f"golden-heldout-{uuid.uuid4().hex[:12]}"
        if heldout
        else f"golden-{version.skill_id}-v{version.version}-{uuid.uuid4().hex[:6]}"
    )
    from contracts.run import RunManifest

    pinned = RunManifest(
        model_version=model_version or "m4-harness",
        index_snapshot_id=snapshot_id,
        library_commit=snapshot_id,
    )
    request = task_spec.get("request") or (goal.context if goal else None)
    orch = _start_orchestrator(runs_root / "golden-runs")
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
        golden_path=reported_path,
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
    factory: object | None = None,
) -> GoldenResult:
    with _using_factory(factory):
        return _run_goal_fixture(
            golden_dir,
            runs_root=runs_root,
            eval_store=eval_store,
            snapshot_id=snapshot_id,
            model_version=model_version,
        )


def _run_goal_fixture(
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
    orch = _start_orchestrator(
        runs_root / "golden-runs",
        policy=_policy_for_mea_fixture(goal, task_spec),
    )
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
    factory: object | None = None,
) -> GoldenReport:
    with _using_factory(factory):
        return _select_and_run_gate(
            version,
            runs_root=runs_root,
            golden_root=golden_root,
            golden_dir=golden_dir,
            require_task_class_gate=require_task_class_gate,
            require_fixture=require_fixture,
            extra_golden_dirs=extra_golden_dirs,
        )


def _select_and_run_gate(
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
    factory: object | None = None,
) -> GoldenReport:
    with _using_factory(factory):
        return _run_seed_library_gate(
            store,
            golden_root,
            runs_root=runs_root,
            log_path=log_path,
            skill_ids=skill_ids,
        )


def _run_seed_library_gate(
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


def _heldout_fixture_dirs(split_root: Path, task_class: str) -> list[Path]:
    class_root = split_root / task_class
    if not class_root.is_dir():
        raise HeldoutManifestError("held-out split has no fixtures for this task class")
    found = [
        child
        for child in sorted(p for p in class_root.iterdir() if p.is_dir())
        if not child.name.startswith("_")
        and ((child / "task.json").exists() or (child / "goal.json").exists())
    ]
    if not found:
        raise HeldoutManifestError("held-out split has no fixtures for this task class")
    return found


def _heldout_skill_id(fixture_dir: Path) -> str:
    """``expected_skill_id`` from ``task.json``. Never the directory name."""

    task_path = fixture_dir / "task.json"
    if not task_path.is_file():
        raise HeldoutManifestError(
            "held-out fixture has no task.json expected_skill_id; "
            "refusing to infer a skill from the directory name"
        )
    try:
        spec = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise HeldoutManifestError("held-out fixture task.json is unreadable") from None
    skill_id = spec.get("expected_skill_id") if isinstance(spec, dict) else None
    if not isinstance(skill_id, str) or not skill_id.strip():
        raise HeldoutManifestError(
            "held-out fixture has no task.json expected_skill_id; "
            "refusing to infer a skill from the directory name"
        )
    return skill_id


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _under_git_checkout(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _check_heldout_outputs(
    runs_root: Path,
    split_root: Path,
    *,
    eval_store: EvalStore | None,
    forbidden_roots: Iterable[Path | str],
) -> None:
    """Held-out run state never lands in a checkout, the split or a forbidden root.

    Same realpath and ``.git``-ancestor rule as ``verify_heldout_tree`` and the operator
    audit store. Messages carry no path.
    """

    real_runs = Path(os.path.realpath(runs_root))
    if _under_git_checkout(real_runs):
        raise HeldoutManifestError("held-out runs root must be outside any git checkout")
    if _inside(real_runs, split_root) or _inside(split_root, real_runs):
        raise HeldoutManifestError("held-out runs root must not overlap the held-out split")
    for forbidden in forbidden_roots:
        if _inside(real_runs, Path(os.path.realpath(forbidden))):
            raise HeldoutManifestError("held-out runs root is inside a forbidden root")
    store_path = getattr(eval_store, "path", None)
    if store_path is not None and _under_git_checkout(Path(os.path.realpath(store_path))):
        raise HeldoutManifestError("held-out eval store must be outside any git checkout")


def run_heldout_suite(
    *,
    manifest_path: Path,
    heldout_root: Path,
    task_class: str,
    skills_root: Path,
    runs_root: Path,
    eval_store: EvalStore | None = None,
    snapshot_id: str | None = None,
    model_version: str | None = None,
    factory: object | None = None,
    forbidden_roots: Iterable[Path | str] = (),
) -> GoldenReport:
    """Run a private held-out split (REC-009). Fails closed before anything runs.

    * ``verify_heldout_tree`` checks the decrypted root against the public manifest
      (hash and count) and refuses a root inside a git checkout. Nothing is discovered:
      both paths come from the caller.
    * ``runs_root`` (and the eval store, if given) must be outside any git checkout,
      must not overlap the split, and must not sit inside ``forbidden_roots`` (for
      example an API root that holds tenant runs roots). Per-fixture run state is
      created in a scratch dir there and removed after each fixture.
    * Fixture dirs (``<root>/<task_class>/h001`` …) may have any neutral name. The skill
      under test is ``task.json`` ``expected_skill_id``; a fixture without one refuses the
      whole run, so no skill is ever inferred from a held-out dir name.
    * Every result is masked at the source (see ``run_golden_for_skill(heldout=True)``),
      so ``GoldenReport.write``, ``EvalService.suite_payload`` and the eval store carry no
      fixture path, dir name, or skill-bearing run id.
    """

    split_root = Path(os.path.realpath(heldout_root))
    _check_heldout_outputs(
        runs_root, split_root, eval_store=eval_store, forbidden_roots=forbidden_roots
    )
    verify_heldout_tree(manifest_path, heldout_root)
    plan = [(d, _heldout_skill_id(d)) for d in _heldout_fixture_dirs(split_root, task_class)]
    store = SkillStore(skills_root)
    loaded = [v for v, _st, _stats in store.iter_loaded() if v.task_class == task_class]
    report = GoldenReport()
    with _using_factory(factory):
        for fixture_dir, skill_id in plan:
            matches = [v for v in loaded if v.skill_id == skill_id]
            if not matches:
                report.results.append(
                    GoldenResult(
                        skill_id=skill_id,
                        version=0,
                        golden_path=HELDOUT_PATH_MASK,
                        passed=False,
                        terminal=None,
                        run_id="",
                        detail="no skill version available for held-out fixture",
                    )
                )
                continue
            version = max(matches, key=lambda v: v.version)
            report.results.append(
                run_golden_for_skill(
                    version,
                    fixture_dir,
                    runs_root=runs_root,
                    snapshot_id=snapshot_id,
                    model_version=model_version,
                    eval_store=eval_store,
                    heldout=True,
                )
            )
    return report


class PublicGoldenPathError(ValueError):
    """A public golden path that is not inside a git checkout (REC-009)."""


def require_public_golden_path(path: Path) -> None:
    """Refuse a public golden root or dir whose realpath is outside every git checkout.

    The exact complement of :func:`~recertia.evals.heldout_manifest.verify_heldout_tree`,
    which refuses a held-out root *inside* a checkout: public goldens live in the
    repository, so a decrypted held-out split can never be run through the public path
    unverified and unmasked. The message carries no path.
    """

    if not _under_git_checkout(Path(os.path.realpath(path))):
        raise PublicGoldenPathError(
            "eval run refuses a golden path outside a git checkout: public goldens live "
            "in the repository; run a decrypted held-out split with "
            "recertia.evals.golden.run_heldout_suite"
        )


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
    factory: object | None = None,
) -> GoldenReport:
    with _using_factory(factory):
        return _run_eval_suite(
            task_class=task_class,
            golden_root=golden_root,
            skills_root=skills_root,
            runs_root=runs_root,
            eval_store=eval_store,
            snapshot_id=snapshot_id,
            model_version=model_version,
            golden_dir=golden_dir,
        )


def _run_eval_suite(
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

