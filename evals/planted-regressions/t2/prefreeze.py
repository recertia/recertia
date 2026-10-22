#!/usr/bin/env python3
"""Prove each T2 planted successor is refused by the golden gate under the container backend.

Writes PREFREEZE-REFUSALS.json, then MANIFEST.sha256 last. Scratch roots only — never
evals/golden/_promotion_logs, skills/, or .recertia/.

``attempts`` is not measured here: each case calls ``promote_to_approved`` exactly once and
the gate exposes no replay or retry count, so every row records ``"attempts": null`` with
``"attempts_measured": false``. The T2 harness must measure attempts (and first-attempt and
final outcomes) itself; nothing in this file is evidence of an attempt count.

The frozen results are path-independent: the scratch root (a random ``mkdtemp`` directory)
is written as ``<scratch>`` in every message, and each row carries the structured
``refusal_kind``, ``failing_fixtures`` and ``failing_count`` so no consumer has to parse
free text. Since REC-009 a refusal is decision-only (no fixture ids in the message), so on
such a gate ``failing_fixtures`` is null and ``failing_count`` comes from the error.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
SET_DIR = Path(__file__).resolve().parent
GOLDEN_ROOT = ROOT / "evals" / "golden"
SKILLS_ROOT = ROOT / "skills"
ALLOWED_KINDS = frozenset({"golden_gate", "predecessor_non_regression"})
SCRATCH_PREFIX = "recertia-t2-prefreeze-"
SCRATCH_PLACEHOLDER = "<scratch>"
_SCRATCH_RE = re.compile(
    r"(?:/private)?(?:/var/folders/[^\s'\"]+?|/var/tmp|/tmp)/" + re.escape(SCRATCH_PREFIX) + r"[A-Za-z0-9_]+"
)
SCRATCH_PATHS_NOTE = (
    "the per-run mkdtemp scratch root is written as <scratch>; the frozen results "
    "contain no absolute temp paths"
)
_FAILING_RE = re.compile(r"failing fixtures=(\[[^\]]*\])")
CASES = (
    "add-editorconfig",
    "add-gitignore-entry",
    "add-license-mit",
    "add-makefile-target",
    "add-pytest-config",
    "add-readme-section",
    "bump-action-checkout",
    "ensure-src-layout",
    "pin-python-version",
    "strip-trailing-whitespace",
)
ATTEMPTS_NOT_MEASURED_REASON = (
    "not measured: prefreeze.py calls promote_to_approved once per case and the gate "
    "exposes no replay or retry count; the T2 harness must measure attempts and the "
    "first-attempt and final outcomes"
)
EDIT_SUMMARIES = {
    "add-editorconfig": "omit the [*.py] section from .editorconfig",
    "add-gitignore-entry": "append *.pyo instead of *.pyc",
    "add-license-mit": "write BSD License text instead of MIT License",
    "add-makefile-target": "write a check: target instead of test:",
    "add-pytest-config": "write addopts instead of testpaths in pytest.ini",
    "add-readme-section": "append ## Setup instead of ## Installation",
    "bump-action-checkout": "pin actions/checkout to v2 instead of v4",
    "ensure-src-layout": "create src/other_pkg instead of src/demo_pkg",
    "pin-python-version": "pin 3.11 instead of 3.12",
    "strip-trailing-whitespace": "strip only trailing tabs, leaving trailing spaces",
}


def _pin_m1_env() -> None:
    os.environ["RECERTIA_EXECUTION_BACKEND"] = "container"
    os.environ["RECERTIA_GOLDEN_BACKEND"] = "container"
    os.environ["RECERTIA_CONTAINER_RUNTIME"] = "docker"
    os.environ["RECERTIA_WORKDIR_WORLD_WRITE"] = "1"
    os.environ["RECERTIA_MODEL_PROVIDER"] = "stub"
    os.environ["RECERTIA_MODEL_ID"] = "stub"
    os.environ.pop("RECERTIA_LEDGER_HMAC_KEY", None)
    os.environ.pop("RECERTIA_FORCE_LOCAL", None)


def _code_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _date_pt() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _criteria_identical(skill_id: str) -> str:
    v1 = json.loads((SKILLS_ROOT / skill_id / "v1" / "version.json").read_text(encoding="utf-8"))
    v2 = json.loads((SET_DIR / skill_id / "version.json").read_text(encoding="utf-8"))
    if v1["certification_criteria"] != v2["certification_criteria"]:
        return "changed"
    return "identical"


def _refusal_kind(message: str) -> str:
    text = message.lower()
    if "predecessor non-regression" in text:
        return "predecessor_non_regression"
    if "golden gate" in text:
        return "golden_gate"
    if "skill not eligible" in text or "lint" in text:
        return "lint"
    if "applicability gate" in text:
        return "applicability"
    if "lifecycle" in text:
        return "lifecycle"
    return "other"


def mask_scratch_paths(text: str, scratch: Path | None = None) -> str:
    """Replace the random scratch root (and any prefreeze scratch dir) with ``<scratch>``."""

    if scratch is not None:
        text = text.replace(str(scratch), SCRATCH_PLACEHOLDER)
    return _SCRATCH_RE.sub(SCRATCH_PLACEHOLDER, text)


def refusal_from_error(exc: BaseException) -> dict[str, Any]:
    """Structured refusal: kind, failing fixture ids (when the gate names them), count.

    Uses the error's own ``refusal_kind`` / ``failing_count`` when it has them (REC-009
    decision-only errors) and falls back to the pre-REC-009 message text otherwise.
    """

    message = str(exc)
    structured_kind = getattr(exc, "refusal_kind", None)
    kind = structured_kind if isinstance(structured_kind, str) else _refusal_kind(message)
    failing: list[str] | None = None
    match = _FAILING_RE.search(message)
    if match:
        failing = [str(item) for item in ast.literal_eval(match.group(1))]
    count: int | None = None
    if failing is not None:
        count = len(failing)
    elif kind in ALLOWED_KINDS and isinstance(getattr(exc, "failing_count", None), int):
        count = int(getattr(exc, "failing_count"))
    return {"refusal_kind": kind, "failing_fixtures": failing, "failing_count": count}


def promote_kwargs(scratch: Path) -> dict[str, Any]:
    """Extra ``promote_to_approved`` kwargs for the gate in this checkout.

    Since REC-009 the gate requires an operator audit sink (``audit=``) outside any
    checkout; before it there is no such parameter.
    """

    from recertia.memory.procedural.promote import promote_to_approved

    if "audit" not in inspect.signature(promote_to_approved).parameters:
        return {}
    from recertia.review.promotion_audit import OperatorAuditStore  # type: ignore[import-not-found,unused-ignore]

    return {"audit": OperatorAuditStore(scratch / "operator-audit")}


def case_row(
    *,
    skill_id: str,
    refusal_kind: str,
    backend_used: str,
    criteria_diff: str,
    executor_id: str,
    code_sha: str,
    date_pt: str,
    message: str,
    failing_fixtures: list[str] | None = None,
    failing_count: int | None = None,
) -> dict[str, Any]:
    """One PREFREEZE-REFUSALS.json row. ``attempts`` is explicitly not measured (None)."""

    row: dict[str, Any] = {
        "id": skill_id,
        "edit_summary": EDIT_SUMMARIES[skill_id],
        "refusal_kind": refusal_kind,
        "failing_fixtures": failing_fixtures,
        "failing_count": failing_count,
        "backend_used": backend_used,
        "criteria_diff": criteria_diff,
        "executor_id": executor_id,
        "code_sha": code_sha,
        "date_pt": date_pt,
        "attempts": None,
        "attempts_measured": False,
        "message": mask_scratch_paths(message),
    }
    if skill_id == "add-editorconfig":
        row["note"] = (
            "predecessor fixture is the same directory as the candidate's and is "
            "deduplicated; refusal is the candidate failing"
        )
    return row


def case_row_problems(row: dict[str, Any]) -> list[str]:
    """Reasons a row must not be frozen (empty when it is acceptable)."""

    problems: list[str] = []
    if row.get("refusal_kind") not in ALLOWED_KINDS:
        problems.append(f"refusal_kind {row.get('refusal_kind')!r} not allowed")
    if row.get("backend_used") != "container":
        problems.append(f"backend_used {row.get('backend_used')!r} is not container")
    if row.get("criteria_diff") != "identical":
        problems.append("certification_criteria changed")
    measured = row.get("attempts_measured")
    attempts = row.get("attempts")
    if measured is True:
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            problems.append(f"attempts_measured but attempts={attempts!r}")
    elif measured is False:
        if attempts is not None:
            problems.append(f"attempts={attempts!r} recorded without a measurement")
    else:
        problems.append("attempts_measured missing")
    if _SCRATCH_RE.search(json.dumps(row)):
        problems.append("row contains an unmasked scratch path")
    failing = row.get("failing_fixtures")
    if failing is not None and row.get("failing_count") != len(failing):
        problems.append("failing_count does not match failing_fixtures")
    return problems


def write_manifest() -> Path:
    dest = SET_DIR / "MANIFEST.sha256"
    rows: list[str] = []
    for path in sorted(p for p in SET_DIR.rglob("*") if p.is_file()):
        if path.name == "MANIFEST.sha256":
            continue
        rel = path.relative_to(SET_DIR).as_posix()
        rows.append(f"{_sha256_file(path)}  {rel}")
    dest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return dest


def prove() -> dict[str, Any]:
    _pin_m1_env()
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))

    from recertia.memory.procedural.promote import PromotionError, promote_to_approved
    from recertia.memory.procedural.store import SkillStore
    from recertia.review.gate import default_golden_factory
    from recertia.settings import load_settings
    from recertia.solver import container as container_mod
    from recertia.solver.container import (
        configured_backend,
        container_runtime,
        ensure_execution_ready,
        probe_container_runtime,
    )
    from recertia.solver.executor import ContainerExecutor
    from recertia.solver.sandbox import SandboxError

    settings = load_settings()
    if settings.golden_backend != "container" or settings.execution_backend != "container":
        raise SystemExit(
            f"M1 pin not met: golden_backend={settings.golden_backend!r} "
            f"execution_backend={settings.execution_backend!r}"
        )
    runtime = container_runtime()
    if runtime is None:
        raise SystemExit(
            "STOP: no Docker/Podman on PATH; the M1 container pin cannot be met. "
            "Do not fall back to local execution."
        )
    try:
        ensure_execution_ready()
        probe_container_runtime(timeout_s=90)
    except SandboxError as exc:
        raise SystemExit(
            f"STOP: container runtime is not usable ({exc}); the M1 pin cannot be met. "
            "Do not fall back to local execution."
        ) from exc

    factory = default_golden_factory()
    if not isinstance(factory.executor, ContainerExecutor):
        raise SystemExit(
            f"STOP: default_golden_factory() built {type(factory.executor).__name__}, "
            "not ContainerExecutor"
        )
    executor_id = f"{type(factory.executor).__module__}.{type(factory.executor).__name__}"

    observed: list[str] = []
    orig_run = container_mod.run_configured_command

    def _wrapped_run(command: str, **kwargs: Any) -> Any:
        backend = configured_backend()
        observed.append(backend)
        if backend != "container":
            raise RuntimeError(f"run_configured_command used {backend!r}, not container")
        return orig_run(command, **kwargs)

    container_mod.run_configured_command = _wrapped_run  # type: ignore[method-assign]

    code_sha = _code_sha()
    date_pt = _date_pt()
    cases: list[dict[str, Any]] = []

    scratch = Path(tempfile.mkdtemp(prefix="recertia-t2-prefreeze-"))
    try:
        store_root = scratch / "skills"
        log_dir = scratch / "promotion_logs"
        runs_root = scratch / "promote-runs"
        store_root.mkdir()
        log_dir.mkdir()
        runs_root.mkdir()
        store = SkillStore(store_root)

        for skill_id in CASES:
            v1_src = SKILLS_ROOT / skill_id / "v1"
            v1_dest = store_root / skill_id / "v1"
            shutil.copytree(v1_src, v1_dest)
            v2 = json.loads((SET_DIR / skill_id / "version.json").read_text(encoding="utf-8"))
            from contracts.skill import SkillVersion

            store.write_candidate(SkillVersion.model_validate(v2))
            observed.clear()
            refusal: dict[str, Any] = {
                "refusal_kind": "other",
                "failing_fixtures": None,
                "failing_count": None,
            }
            message = ""
            try:
                promote_to_approved(
                    store,
                    skill_id,
                    2,
                    golden_dir=GOLDEN_ROOT / "repo-chore" / skill_id,
                    golden_root=GOLDEN_ROOT,
                    runs_root=runs_root,
                    log_dir=log_dir,
                    repo_root=ROOT,
                    factory=factory,
                    **promote_kwargs(scratch),
                )
                message = "promote_to_approved returned success"
            except PromotionError as exc:
                message = mask_scratch_paths(str(exc), scratch)
                refusal = refusal_from_error(exc)
            backends = sorted(set(observed))
            backend_used = backends[0] if len(backends) == 1 else ",".join(backends) or "none"
            cases.append(
                case_row(
                    skill_id=skill_id,
                    refusal_kind=refusal["refusal_kind"],
                    failing_fixtures=refusal["failing_fixtures"],
                    failing_count=refusal["failing_count"],
                    backend_used=backend_used,
                    criteria_diff=_criteria_identical(skill_id),
                    executor_id=executor_id,
                    code_sha=code_sha,
                    date_pt=date_pt,
                    message=message,
                )
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            subprocess.run(["chmod", "-R", "a+rwX", str(scratch)], check=False)
            shutil.rmtree(scratch, ignore_errors=True)

    bad = [{**row, "problems": case_row_problems(row)} for row in cases if case_row_problems(row)]
    payload = {
        "epsilon": 0.0,
        "golden_backend": "container",
        "execution_backend": "container",
        "runtime": runtime,
        "code_sha": code_sha,
        "date_pt": date_pt,
        "not_measured": {"attempts": ATTEMPTS_NOT_MEASURED_REASON},
        "scratch_paths": SCRATCH_PATHS_NOTE,
        "cases": cases,
    }
    (SET_DIR / "PREFREEZE-REFUSALS.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    if bad:
        detail = json.dumps(bad, indent=2)
        raise SystemExit(f"STOP: cases not refused with an allowed kind / container backend:\n{detail}")
    return payload


def main() -> int:
    prove()
    manifest = write_manifest()
    print(f"MANIFEST.sha256 { _sha256_file(manifest) }")
    print(f"wrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
