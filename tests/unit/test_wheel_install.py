"""The built wheel ships every non-Python file under src/recertia, and the API starts from an install of it.

The wheel is built offline from a copy of the source (``pip wheel --no-build-isolation
--no-deps --no-index``), installed with ``--no-deps --no-index`` into a venv created with
``--system-site-packages``, and ``create_app`` runs there on a fresh root. Nothing touches
the network or the checkout; everything lives under ``tmp_path``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "recertia"
# Copied for the build: what [project] and [tool.setuptools] read.
_BUILD_INPUTS = ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "src", "contracts")
_STEP_TIMEOUT_S = 120


def _require_build_tools() -> None:
    missing = [m for m in ("pip", "setuptools") if importlib.util.find_spec(m) is None]
    if not missing:
        return
    reason = f"wheel build needs {', '.join(missing)} in this environment (dev extra)"
    if os.environ.get("CI"):
        pytest.fail(f"{reason}; CI must run this test, not skip it")
    pytest.skip(reason)


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=_STEP_TIMEOUT_S, check=False
    )
    assert proc.returncode == 0, f"{cmd[:4]} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return proc


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PIP_", "VIRTUAL_ENV"))}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INDEX"] = "1"
    return env


def _package_data_files() -> list[str]:
    found = []
    for path in sorted(_PACKAGE.rglob("*")):
        if not path.is_file() or path.suffix in {".py", ".pyc"} or "__pycache__" in path.parts:
            continue
        found.append(f"recertia/{path.relative_to(_PACKAGE).as_posix()}")
    return found


def _record(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/RECORD"))
        rows = csv.reader(io.StringIO(zf.read(name).decode("utf-8")))
        record = {row[0]: row[1] for row in rows if row}
        for path, digest in record.items():
            if digest:
                algo, _, want = digest.partition("=")
                got = base64.urlsafe_b64encode(hashlib.new(algo, zf.read(path)).digest())
                assert got.rstrip(b"=").decode() == want, path
    return record


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require_build_tools()
    base = tmp_path_factory.mktemp("wheel")
    src = base / "src"
    src.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build")
    for name in _BUILD_INPUTS:
        item = _REPO / name
        if item.is_dir():
            shutil.copytree(item, src / name, ignore=ignore)
        else:
            shutil.copy2(item, src / name)
    dist = base / "dist"
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--no-index"]
    _run([*cmd, "--wheel-dir", str(dist), str(src)], cwd=base, env=_clean_env())
    wheels = list(dist.glob("recertia-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def test_wheel_record_lists_every_package_data_file(built_wheel: Path) -> None:
    expected = _package_data_files()
    assert any(name.endswith(".sql") for name in expected), expected
    missing = sorted(set(expected) - set(_record(built_wheel)))
    assert missing == [], f"not in the wheel RECORD: {missing}"


@pytest.fixture(scope="module")
def installed_python(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A venv with the wheel installed (no deps); returns its python and its site-packages."""
    base = tmp_path_factory.mktemp("installed")
    venv_dir = base / "venv"
    env = _clean_env()
    _run(
        [sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(venv_dir)],
        cwd=base,
        env=env,
    )
    bindir = "Scripts" if os.name == "nt" else "bin"
    child = venv_dir / bindir / ("python.exe" if os.name == "nt" else "python")
    child_site = _run(
        [str(child), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], cwd=base, env=env
    ).stdout.strip()
    # When this interpreter is itself a venv, --system-site-packages reaches its base, not
    # the venv holding fastapi and the other runtime deps; add those paths after the child's.
    parent_paths = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    (Path(child_site) / "_parent_site.pth").write_text("\n".join(sorted(parent_paths)) + "\n")
    pip = [sys.executable, "-m", "pip", "--python", str(child), "install", "--no-deps", "--no-index"]
    _run([*pip, str(built_wheel)], cwd=base, env=env)
    return child, Path(child_site)


def _run_in_install(installed: tuple[Path, Path], cwd: Path, body: str) -> str:
    """Run ``body`` in the installed venv, after checking recertia comes from the wheel."""
    child, child_site = installed
    probe = textwrap.dedent(
        f"""
        import pathlib
        import recertia

        where = pathlib.Path(recertia.__file__).resolve()
        assert where.is_relative_to(pathlib.Path({str(child_site)!r}).resolve()), where
        """
    ) + textwrap.dedent(body)
    return _run([str(child), "-c", probe], cwd=cwd, env=_clean_env()).stdout.strip()


def test_api_starts_from_an_installed_wheel(installed_python: tuple[Path, Path], tmp_path: Path) -> None:
    root = tmp_path / "fresh-root"
    out = _run_in_install(
        installed_python,
        tmp_path,
        f"""
        import importlib.metadata
        from recertia.api import create_app

        app = create_app(root=pathlib.Path({str(root)!r}))
        assert app.version == importlib.metadata.version("recertia"), app.version
        print("started", app.version)
        """,
    )
    assert out.startswith("started "), out
