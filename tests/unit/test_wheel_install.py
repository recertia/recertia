"""The built wheel and sdist ship every non-Python file under src/recertia; the API starts from the wheel.

The wheel is built offline from a copy of the source (``pip wheel --no-build-isolation
--no-deps --no-index``); the sdist comes from setuptools' PEP 517 ``build_sdist`` hook on
another copy, also offline. The wheel is installed with ``--no-deps --no-index`` into a venv created with
``--system-site-packages``, and ``create_app`` runs there on a fresh root. Nothing touches
the network or the checkout; everything lives under ``tmp_path``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "recertia"
# Copied for the build: what [project] and [tool.setuptools] read.
_BUILD_INPUTS = ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "src", "contracts")
# Also read by setuptools when present; copied so the test build matches a real one.
_OPTIONAL_BUILD_INPUTS = ("MANIFEST.in", "setup.py", "setup.cfg")
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


def _copy_build_inputs(base: Path) -> Path:
    src = base / "src"
    src.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build")
    optional = [name for name in _OPTIONAL_BUILD_INPUTS if (_REPO / name).exists()]
    for name in (*_BUILD_INPUTS, *optional):
        item = _REPO / name
        if item.is_dir():
            shutil.copytree(item, src / name, ignore=ignore)
        else:
            shutil.copy2(item, src / name)
    return src


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require_build_tools()
    base = tmp_path_factory.mktemp("wheel")
    src = _copy_build_inputs(base)
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
def built_sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require_build_tools()
    base = tmp_path_factory.mktemp("sdist")
    src = _copy_build_inputs(base)
    dist = base / "dist"
    dist.mkdir()
    hook = "import sys; from setuptools import build_meta; build_meta.build_sdist(sys.argv[1])"
    _run([sys.executable, "-c", hook, str(dist)], cwd=src, env=_clean_env())
    sdists = list(dist.glob("recertia-*.tar.gz"))
    assert len(sdists) == 1, sdists
    return sdists[0]


def test_sdist_lists_every_package_data_file(built_sdist: Path) -> None:
    expected = _package_data_files()
    assert any(name.endswith(".sql") for name in expected), expected
    with tarfile.open(built_sdist) as tf:
        members = {m.name.partition("/")[2] for m in tf.getmembers() if m.isfile()}
    missing = sorted(f"src/{name}" for name in expected if f"src/{name}" not in members)
    assert missing == [], f"not in the sdist: {missing}"


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


def test_default_authoring_prior_from_a_wheel_raises_a_named_error(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    # Wheels do not ship policy/; a successful run reaches load_authoring_prior via distill.
    out = _run_in_install(
        installed_python,
        tmp_path,
        """
        from recertia.distill.prior import load_authoring_prior

        try:
            load_authoring_prior()
        except Exception as exc:
            print(type(exc).__name__, getattr(exc, "code", None), exc, sep="|")
        else:
            print("loaded")
        """,
    )
    name, code, message = out.splitlines()[-1].split("|", 2)
    assert (name, code) == ("AuthoringPriorMissing", "authoring_prior_missing"), out
    assert "policy/authoring-prior.json" in message and "source checkout" in message, message
    assert str(installed_python[1].parents[2]) not in message and str(tmp_path) not in message, message


def test_cli_run_from_a_wheel_names_the_missing_prior_without_a_traceback(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    # The installed console script, on the default passing path, which drafts a skill.
    child, child_site = installed_python
    assert _run_in_install(installed_python, tmp_path, 'print("from-wheel")') == "from-wheel"
    script = child.parent / ("recertia.exe" if os.name == "nt" else "recertia")
    spec = {
        "goal": {
            "desired": [{"id": "f", "kind": "file_exists", "path": "output.txt"}],
            "context": "write output.txt",
            "task_class": "repo-chore",
        },
        "script": ["python3 -c \"open('output.txt','w').write('done')\""],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    cmd = [str(script), "run", "--spec", str(spec_path), "--local-exec"]
    for flag, name in (("--runs-root", "runs"), ("--skills-root", "skills"), ("--facts-root", "facts")):
        cmd += [flag, str(tmp_path / name)]
    env = {k: v for k, v in _clean_env().items() if k != "RECERTIA_EXECUTION_BACKEND"}
    proc = subprocess.run(
        cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=_STEP_TIMEOUT_S, check=False
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 7, output
    assert "Traceback" not in output and 'File "' not in output, output
    venv_dir = child_site.parents[2] if os.name != "nt" else child_site.parents[1]
    for absolute in (str(venv_dir), str(child_site), str(tmp_path), "site-packages"):
        assert absolute not in output, (absolute, output)
    assert "authoring_prior_missing: " in proc.stderr, output
    assert "policy/authoring-prior.json" in proc.stderr, output


def test_api_run_from_a_wheel_ends_with_the_missing_prior_code(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    # POST /v1/runs (sync) on the default passing path; the run's error event keeps the code.
    root = tmp_path / "api-root"
    out = _run_in_install(
        installed_python,
        tmp_path,
        f"""
        import json
        import os

        os.environ["RECERTIA_EXECUTION_BACKEND"] = "local"
        from fastapi.testclient import TestClient
        from recertia.api import create_app
        from recertia.api.events import RunEventLog

        root = pathlib.Path({str(root)!r})
        app = create_app(root=root)
        issued = app.state.api_keys.issue(tenant_id="t1", scopes={{"runs", "exec"}}, actor="test-admin")
        body = {{
            "run_id": "wheel-sync",
            "goal": {{
                "desired": [{{"id": "f", "kind": "file_exists", "path": "output.txt"}}],
                "context": "write output.txt",
                "task_class": "repo-chore",
            }},
            "script": ["python3 -c \\"open('output.txt','w').write('done')\\""],
        }}
        with TestClient(app) as client:
            resp = client.post("/v1/runs", json=body, headers={{"X-API-Key": issued.secret}})
        events = RunEventLog(root).list_after("t1", "wheel-sync")
        errors = [e["payload"] for e in events if e["type"] == "error"]
        terminal = resp.json().get("terminal")
        print(json.dumps({{"status": resp.status_code, "terminal": terminal, "errors": errors}}))
        """,
    )
    result = json.loads(out.splitlines()[-1])
    assert result["terminal"] == "error", result
    assert [e["code"] for e in result["errors"]] == ["authoring_prior_missing"], result
    message = result["errors"][0]["message"]
    assert "policy/authoring-prior.json" in message and "source checkout" in message, result
    for absolute in (str(installed_python[1].parents[2]), str(tmp_path), "site-packages"):
        assert absolute not in out, (absolute, out)


def _run_installed_cli(
    installed: tuple[Path, Path], cwd: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    child, _ = installed
    script = child.parent / ("recertia.exe" if os.name == "nt" else "recertia")
    env = {k: v for k, v in _clean_env().items() if k != "RECERTIA_EXECUTION_BACKEND"}
    return subprocess.run(
        [str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_STEP_TIMEOUT_S,
        check=False,
    )


@pytest.mark.parametrize(
    ("flag", "content", "named"),
    [
        ("--spec", "{not json", "spec bad.json is not valid JSON"),
        ("--goal", "{}", "goal bad.json is not a valid goal"),
    ],
    ids=["malformed-spec", "empty-goal"],
)
def test_cli_run_from_a_wheel_rejects_bad_input_without_a_traceback(
    installed_python: tuple[Path, Path], tmp_path: Path, flag: str, content: str, named: str
) -> None:
    # Same input with and without --local-exec: both exit 2 (invalid input) with no traceback.
    _, child_site = installed_python
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    base = ["run", flag, str(bad), "--model", "stub", "--runs-root", str(tmp_path / "runs")]
    local = _run_installed_cli(installed_python, tmp_path, [*base, "--local-exec"])
    default = _run_installed_cli(installed_python, tmp_path, base)
    venv_dir = child_site.parents[2] if os.name != "nt" else child_site.parents[1]
    for proc in (local, default):
        output = proc.stdout + proc.stderr
        assert proc.returncode == 2, output
        assert "Traceback" not in output and 'File "' not in output, output
        for absolute in (str(venv_dir), str(child_site), str(tmp_path), "site-packages"):
            assert absolute not in output, (absolute, output)
    assert local.returncode == default.returncode
    assert local.stderr.startswith(f"invalid_config: {named}"), local.stderr
