"""Import every recertia and contracts module in-process (nightly / bug 12+13).

One subprocess per module is too slow for required ci. This test batches the
walk in the current interpreter, fails on ImportError, and asserts that
importing ``recertia.api.app`` does not write ``.recertia/`` into cwd.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _walk(package_name: str) -> list[str]:
    module = importlib.import_module(package_name)
    names = [package_name]
    if not hasattr(module, "__path__"):
        return names
    for info in pkgutil.walk_packages(module.__path__, package_name + "."):
        names.append(info.name)
    return names


def test_import_workers_on_a_cold_interpreter() -> None:
    """Bug 12: ``import recertia.workers`` used to raise a circular ImportError."""

    # Force a fresh load if a previous test already imported the package.
    for name in list(sys.modules):
        if name == "recertia.workers" or name.startswith("recertia.workers."):
            sys.modules.pop(name)
        if name == "recertia.api" or name.startswith("recertia.api."):
            sys.modules.pop(name)
    imported = importlib.import_module("recertia.workers")
    assert imported.AsyncRunWorker is not None


def test_import_api_app_does_not_write_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in list(sys.modules):
        if name == "recertia.api.app":
            sys.modules.pop(name)
    importlib.import_module("recertia.api.app")
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
