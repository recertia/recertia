"""Plan-lane acceptance tests: structural done-when checks on plan artifacts.

Covers docs/plans/*.program.json (MigrationProgram, ADR-0014) and evals/golden/*/*/goal.json
(Goal, ADR-0010). The Postgres soak never reads these files.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from contracts.goal import DesiredState
from contracts.program import MigrationProgram

REPO = Path(__file__).resolve().parents[2]
PROGRAMS = sorted((REPO / "docs/plans").glob("*.program.json"))
GOALS = sorted((REPO / "evals/golden").glob("*/*/goal.json"))
_PATH_RE = re.compile(r"\b((?:tests|scripts)/[\w./-]+\.py)\b")
HASH_SEEDS = ("0", "1", "12345")


def _load(path: Path) -> MigrationProgram:
    return MigrationProgram.model_validate_json(path.read_text(encoding="utf-8"))


def _desired(prog: MigrationProgram) -> Iterator[tuple[str, list[DesiredState]]]:
    yield "program_bar", prog.program_bar_desired
    for step in prog.steps:
        yield step.step_id, step.goal.desired


def _overlaps(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def test_plan_artifacts_discovered() -> None:
    assert PROGRAMS, "no docs/plans/*.program.json found"
    assert GOALS, "no evals/golden/*/*/goal.json found"


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_every_step_has_done_when(path: Path) -> None:
    prog = _load(path)
    assert prog.program_bar_desired, "program needs a program-level done-when bar"
    for step in prog.steps:
        assert step.goal.desired, f"{step.step_id}: no desired (done-when) criteria"
        assert step.acceptance_gate.terminal_in, f"{step.step_id}: empty acceptance_gate.terminal_in"
        assert step.mutate_paths, f"{step.step_id}: no mutate_paths"


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_ids_ordinals_and_refs(path: Path) -> None:
    prog = _load(path)
    ids = [step.step_id for step in prog.steps]
    assert len(ids) == len(set(ids)), "duplicate step_id"
    assert [step.ordinal for step in prog.steps] == list(range(len(prog.steps)))
    for step in prog.steps:
        crit = [d.id for d in step.goal.desired] + [c.id for c in step.goal.constraints]
        assert len(crit) == len(set(crit)), f"{step.step_id}: duplicate criterion id"
        for dep in step.depends_on:
            assert dep in ids[: step.ordinal], f"{step.step_id}: depends_on {dep!r} is not an earlier step"
    bar = [d.id for d in prog.program_bar_desired] + [c.id for c in prog.program_bar_constraints]
    assert len(bar) == len(set(bar)), "duplicate program_bar criterion id"


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_freeze_vs_mutate_consistent(path: Path) -> None:
    prog = _load(path)
    bar_frozen = [
        v for c in prog.program_bar_constraints if c.kind == "must_not_modify" for v in (c.value or [])
    ]
    for step in prog.steps:
        step_frozen = [
            v for c in step.goal.constraints if c.kind == "must_not_modify" for v in (c.value or [])
        ]
        frozen = list(step.freeze_paths) + bar_frozen + step_frozen
        bad = [(m, f) for m in step.mutate_paths for f in frozen if _overlaps(m, f)]
        assert not bad, f"{step.step_id}: mutate_paths overlap frozen paths {bad}"


@pytest.mark.parametrize("path", PROGRAMS, ids=lambda p: p.name)
def test_referenced_paths_exist(path: Path) -> None:
    prog = _load(path)
    missing: list[tuple[str, str, str]] = []
    for where, desired in _desired(prog):
        for d in desired:
            if d.kind == "command" and d.run:
                missing += [(where, d.id, t) for t in _PATH_RE.findall(d.run) if not (REPO / t).exists()]
            elif d.kind == "file_contains" and d.path and not (REPO / d.path).exists():
                missing.append((where, d.id, d.path))
    assert not missing, missing


# Runs in a fresh interpreter per PYTHONHASHSEED; prints {label: criteria_hash} as JSON.
_HASH_SCRIPT = """
import json, sys
from pathlib import Path
from contracts.goal import Goal, compile_goal
from contracts.program import MigrationProgram
from recertia.nodes._util import criteria_hash
out = {}
for arg in sys.argv[1:]:
    p = Path(arg)
    text = p.read_text(encoding="utf-8")
    if p.name == "goal.json":
        out[arg] = criteria_hash(compile_goal(Goal.model_validate_json(text)))
    else:
        for s in MigrationProgram.model_validate_json(text).steps:
            out[f"{arg}#{s.step_id}"] = criteria_hash(compile_goal(s.goal))
print(json.dumps(out, sort_keys=True))
"""


def _hashes_under_seed(seed: str) -> dict[str, str]:
    env = dict(os.environ, PYTHONHASHSEED=seed)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), str(REPO / "src"), env.get("PYTHONPATH")]))
    args = [str(p.relative_to(REPO)) for p in PROGRAMS + GOALS]
    proc = subprocess.run(
        [sys.executable, "-c", _HASH_SCRIPT, *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"seed {seed}: {proc.stderr}"
    result: dict[str, str] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def test_compile_is_deterministic_across_hash_seeds() -> None:
    """compile_goal + criteria_hash must not depend on PYTHONHASHSEED (ADR-0003 pre-registration)."""
    runs = {seed: _hashes_under_seed(seed) for seed in HASH_SEEDS}
    baseline = runs[HASH_SEEDS[0]]
    expected = len(GOALS) + sum(len(_load(p).steps) for p in PROGRAMS)
    assert len(baseline) == expected, f"hashed {len(baseline)} goals, expected {expected}"
    for seed, hashes in runs.items():
        diff = sorted(k for k in baseline if hashes.get(k) != baseline[k])
        assert not diff, f"criteria_hash differs under PYTHONHASHSEED={seed}: {diff}"
