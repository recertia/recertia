#!/usr/bin/env python3
"""Prove each T2 planted successor is refused by the golden gate under the container backend.

Writes PREFREEZE-REFUSALS.json, then MANIFEST.sha256 last. Scratch roots only — never
evals/golden/_promotion_logs, skills/, or .recertia/.
"""

from __future__ import annotations

import hashlib
import json
import os
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
            attempts = 1
            kind = "other"
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
                )
                message = "promote_to_approved returned success"
            except PromotionError as exc:
                message = str(exc)
                kind = _refusal_kind(message)
            backends = sorted(set(observed))
            backend_used = backends[0] if len(backends) == 1 else ",".join(backends) or "none"
            row = {
                "id": skill_id,
                "edit_summary": EDIT_SUMMARIES[skill_id],
                "refusal_kind": kind,
                "backend_used": backend_used,
                "criteria_diff": _criteria_identical(skill_id),
                "executor_id": executor_id,
                "code_sha": code_sha,
                "date_pt": date_pt,
                "attempts": attempts,
                "message": message,
            }
            if skill_id == "add-editorconfig":
                row["note"] = (
                    "predecessor fixture is the same directory as the candidate's and is "
                    "deduplicated; refusal is the candidate failing"
                )
            cases.append(row)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            subprocess.run(["chmod", "-R", "a+rwX", str(scratch)], check=False)
            shutil.rmtree(scratch, ignore_errors=True)

    bad = [
        row
        for row in cases
        if row["refusal_kind"] not in ALLOWED_KINDS
        or row["backend_used"] != "container"
        or row["criteria_diff"] != "identical"
    ]
    payload = {
        "epsilon": 0.0,
        "golden_backend": "container",
        "execution_backend": "container",
        "runtime": runtime,
        "code_sha": code_sha,
        "date_pt": date_pt,
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
