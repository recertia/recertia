"""Honest must_not_modify: seal path digests at intake, verify via command criterion."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from contracts.criteria import TaskCriterion
from contracts.goal import Constraint, Goal


def path_digest(root: Path, rel: str) -> str:
    """sha256 of a file, or sorted file digests under a directory."""

    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"freeze path escapes workdir: {rel}") from exc
    if not target.exists():
        return hashlib.sha256(b"missing:" + rel.encode()).hexdigest()
    if target.is_file():
        return hashlib.sha256(target.read_bytes()).hexdigest()
    entries: list[tuple[str, str]] = []
    for p in sorted(target.rglob("*")):
        if p.is_file():
            rel_p = str(p.relative_to(root)).replace("\\", "/")
            entries.append((rel_p, hashlib.sha256(p.read_bytes()).hexdigest()))
    blob = json.dumps(entries, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _freeze_script(expected: dict[str, str]) -> str:
    payload = json.dumps(expected, sort_keys=True)
    code = (
        "import hashlib,json,pathlib,sys\n"
        f"exp=json.loads({payload!r})\n"
        "root=pathlib.Path('.').resolve()\n"
        "def dig(rel):\n"
        " p=(root/rel).resolve()\n"
        " try: p.relative_to(root)\n"
        " except ValueError: return 'escape'\n"
        " if not p.exists(): return hashlib.sha256(('missing:'+rel).encode()).hexdigest()\n"
        " if p.is_file(): return hashlib.sha256(p.read_bytes()).hexdigest()\n"
        " ents=[]\n"
        " for f in sorted(p.rglob('*')):\n"
        "  if f.is_file():\n"
        "   ents.append((str(f.relative_to(root)).replace('\\\\','/'), "
        "hashlib.sha256(f.read_bytes()).hexdigest()))\n"
        " return hashlib.sha256(json.dumps(ents,separators=(',',':')).encode()).hexdigest()\n"
        "bad=[r for r,h in exp.items() if dig(r)!=h]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    # shlex.quote keeps real newlines inside single quotes. Python repr encodes
    # them as backslash-n, which sh does not turn back into newlines, so
    # ``python -c`` then sees invalid source.
    return "python -c " + shlex.quote(code)


def _paths(constraint: Constraint) -> list[str]:
    if isinstance(constraint.value, list):
        return [str(p) for p in constraint.value]
    if isinstance(constraint.value, str):
        return [constraint.value]
    raise ValueError(f"must_not_modify {constraint.id!r} requires str or list[str]")


def seal_must_not_modify_criteria(
    criteria: list[TaskCriterion],
    *,
    goal: Goal | None,
    workdir: Path,
) -> list[TaskCriterion]:
    """Replace placeholder freeze checks with digest-sealed command criteria."""

    if goal is None:
        return criteria
    freezes = [c for c in goal.constraints if c.kind == "must_not_modify"]
    if not freezes:
        return criteria

    by_id = {c.id: c for c in criteria}
    for constraint in freezes:
        paths = _paths(constraint)
        expected = {p: path_digest(workdir, p) for p in paths}
        sealed = TaskCriterion(
            id=constraint.id,
            kind="command",
            run=_freeze_script(expected),
            expect_exit=0,
            weight=constraint.weight,
            source="caller",
            preregistered=True,
            timeout_s=300,
        )
        by_id[constraint.id] = sealed
    out: list[TaskCriterion] = []
    seen: set[str] = set()
    for c in criteria:
        out.append(by_id.get(c.id, c))
        seen.add(c.id)
    for constraint in freezes:
        if constraint.id not in seen:
            out.append(by_id[constraint.id])
    return out
