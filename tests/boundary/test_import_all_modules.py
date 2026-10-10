"""Import every recertia and contracts module in-process (nightly / bug 12+13).

One subprocess per module is too slow for required ci. This test batches the
walk in the current interpreter, fails on ImportError, and uses a short
subprocess only for the two bugs that need a cold interpreter (circular import
and import-time cwd write).
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _walk(package_name: str) -> list[str]:
    module = importlib.import_module(package_name)
    names = [package_name]
    if not hasattr(module, "__path__"):
        return names
    for info in pkgutil.walk_packages(module.__path__, package_name + "."):
        names.append(info.name)
    return names


def _cold(code: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_import_workers_on_a_cold_interpreter(tmp_path: Path) -> None:
    """Bug 12: ``import recertia.workers`` used to raise a circular ImportError."""

    proc = _cold("import recertia.workers; print(recertia.workers.AsyncRunWorker)", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "AsyncRunWorker" in proc.stdout


def test_import_api_app_does_not_write_cwd(tmp_path: Path) -> None:
    proc = _cold("import recertia.api.app", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert not (tmp_path / ".recertia").exists()


def test_import_all_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    names = _walk("contracts") + _walk("recertia")
    assert len(names) > 50
    failures: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        warnings.filterwarnings(
            "ignore",
            category=DeprecationWarning,
            module=r"starlette\..*|fastapi\..*|pydantic\..*|uvicorn\..*",
        )
        for name in names:
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 — collect every failure
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert failures == [], "import failures:\n" + "\n".join(failures)
    assert not (tmp_path / ".recertia").exists(), ".recertia/ was created during import-all"
