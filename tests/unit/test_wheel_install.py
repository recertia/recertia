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


def _run_in_install(
    installed: tuple[Path, Path],
    cwd: Path,
    body: str,
    *,
    env: dict[str, str] | None = None,
) -> str:
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
    run_env = env if env is not None else _clean_env()
    return _run([str(child), "-c", probe], cwd=cwd, env=run_env).stdout.strip()


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


def test_console_is_served_from_an_installed_wheel(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    """Packaged console static files are served under /console with no checkout present."""

    root = tmp_path / "console-root"
    # cwd is tmp_path: no repo-root console/, no source checkout.
    out = _run_in_install(
        installed_python,
        tmp_path,
        f"""
        from fastapi.testclient import TestClient
        from recertia.api import create_app

        app = create_app(root=pathlib.Path({str(root)!r}))
        client = TestClient(app)
        for path in ("/console", "/console/"):
            page = client.get(path)
            assert page.status_code == 200, (path, page.status_code, page.text[:200])
            assert b"Recertia" in page.content or "Recertia" in page.text
        for name in ("app.js", "index.html", "mark.svg", "styles.css"):
            asset = client.get(f"/console/assets/{{name}}")
            assert asset.status_code == 200, (name, asset.status_code)
            assert len(asset.content) > 0
        print("console-ok")
        """,
    )
    assert out.endswith("console-ok"), out


def test_console_static_in_wheel_record_is_exactly_the_four_files(built_wheel: Path) -> None:
    prefix = "recertia/console/static/"
    names = sorted(
        path[len(prefix) :]
        for path in _record(built_wheel)
        if path.startswith(prefix) and not path.endswith("/")
    )
    assert names == ["app.js", "index.html", "mark.svg", "styles.css"], names


def test_console_served_without_auth_while_v1_still_requires_it(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    """Packaged /console needs no auth; /v1/runs still requires an API key.

    API-key auth is always wired: ``create_app`` opens ``ApiKeyStore``
    (``src/recertia/api/__init__.py``) and ``GET /v1/runs`` depends on
    ``require_runs`` → ``resolve_principal`` (``identity.py``), which raises
    ``Unauthorized`` when no key is presented. Console-session auth is a
    separate switch (``RECERTIA_CONSOLE_AUTH``, default ``off``); this test
    forces it off so only the API-key path is live, then proves a positive
    control with an issued key.
    """

    root = tmp_path / "auth-root"
    env = _clean_env()
    # Scrub any host leak of the console-auth switch; API-key auth has no off switch.
    env.pop("RECERTIA_CONSOLE_AUTH", None)
    env["RECERTIA_CONSOLE_AUTH"] = "off"
    out = _run_in_install(
        installed_python,
        tmp_path,
        f"""
        import os

        os.environ["RECERTIA_CONSOLE_AUTH"] = "off"
        from fastapi.testclient import TestClient
        from recertia.api import create_app
        from recertia.api.console_auth import auth_mode

        assert auth_mode() == "off", auth_mode()
        app = create_app(root=pathlib.Path({str(root)!r}))
        assert app.state.api_keys is not None
        client = TestClient(app)
        for path in ("/console", "/console/"):
            page = client.get(path)
            assert page.status_code == 200, (path, page.status_code)
        for name in ("app.js", "index.html", "mark.svg", "styles.css"):
            asset = client.get(f"/console/assets/{{name}}")
            assert asset.status_code == 200, (name, asset.status_code)
            assert len(asset.content) > 0
        no_key = client.get("/v1/runs")
        assert no_key.status_code in {{401, 403}}, no_key.status_code
        issued = app.state.api_keys.issue(tenant_id="t1", scopes={{"runs"}}, actor="test-admin")
        with_key = client.get("/v1/runs", headers={{"X-API-Key": issued.secret}})
        assert with_key.status_code == 200, (with_key.status_code, with_key.text[:200])
        print("auth-boundary-ok", "no-key", no_key.status_code, "with-key", with_key.status_code)
        """,
        env=env,
    )
    assert "auth-boundary-ok" in out, out
    assert "with-key 200" in out, out
    assert "no-key 401" in out or "no-key 403" in out, out
    print(out.splitlines()[-1])


def test_heldout_from_a_wheel_raises_a_named_error_when_evals_is_missing(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    # Wheels do not ship evals/; a successful lookup reaches public_evals_root.
    out = _run_in_install(
        installed_python,
        tmp_path,
        """
        from recertia.evals.heldout import public_evals_root

        try:
            public_evals_root()
        except Exception as exc:
            print(type(exc).__name__, getattr(exc, "code", None), exc, sep="|")
        else:
            print("found")
        """,
    )
    name, code, message = out.splitlines()[-1].split("|", 2)
    assert (name, code) == ("EvalsRootMissing", "evals_root_missing"), out
    assert "evals/" in message and "source checkout" in message, message
    assert str(installed_python[1].parents[2]) not in message and str(tmp_path) not in message, message


def test_default_authoring_prior_from_a_wheel_loads_the_packaged_default(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    """Installed wheel loads the packaged default prior (not checkout/cwd/cache)."""

    child, child_site = installed_python
    # Isolate the lru_cache and prove the resource lives under the wheel site-packages.
    out = _run_in_install(
        installed_python,
        tmp_path,
        f"""
        import json
        from importlib.resources import files
        from recertia.distill import prior as prior_mod

        prior_mod.load_authoring_prior.cache_clear()
        loaded = prior_mod.load_authoring_prior()
        prior_mod.load_authoring_prior.cache_clear()
        again = prior_mod.load_authoring_prior()
        resource = files("recertia").joinpath("authoring-prior.json")
        assert resource.is_file(), resource
        resource_path = pathlib.Path(str(resource)).resolve()
        site = pathlib.Path({str(child_site)!r}).resolve()
        assert resource_path.is_relative_to(site), (resource_path, site)
        assert loaded.version == "ap-2026.08.1", loaded.version
        assert again.version == loaded.version
        assert loaded.require_parameter_or_recurrence is True
        assert loaded.max_steps == 12
        print(
            "prior-ok",
            loaded.version,
            loaded.max_steps,
            json.dumps(list(loaded.notes)[:1]),
        )
        """,
    )
    assert "prior-ok ap-2026.08.1 12" in out, out
    assert str(tmp_path) not in out
    assert "policy/authoring-prior.json" not in out


def test_cli_run_from_a_wheel_reaches_distill_with_the_packaged_prior(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    """Installed console script completes the default path through distill (exit 0)."""

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
    runs_root = tmp_path / "runs"
    cmd = [str(script), "run", "--spec", str(spec_path), "--local-exec"]
    for flag, name in (("--runs-root", "runs"), ("--skills-root", "skills"), ("--facts-root", "facts")):
        cmd += [flag, str(tmp_path / name)]
    env = {k: v for k, v in _clean_env().items() if k != "RECERTIA_EXECUTION_BACKEND"}
    proc = subprocess.run(
        cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=_STEP_TIMEOUT_S, check=False
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "terminal=solved" in proc.stdout, output
    assert "authoring_prior_missing" not in output, output
    assert "Traceback" not in output, output
    # Distill node ran (packaged prior loaded); route_entries records it.
    import sqlite3

    con = sqlite3.connect(runs_root / "runtime.db")
    routes = [json.loads(row[0]) for row in con.execute("select entry_json from route_entries")]
    distill_routes = [r for r in routes if r.get("node") == "distill"]
    assert distill_routes, routes
    print("cli-distill-ok", proc.stdout.strip().splitlines()[-1], distill_routes[0]["route"])


def test_api_run_from_a_wheel_reaches_distill_with_the_packaged_prior(
    installed_python: tuple[Path, Path], tmp_path: Path
) -> None:
    """Authenticated POST /v1/runs reaches a successful terminal and distill output."""

    root = tmp_path / "api-root"
    out = _run_in_install(
        installed_python,
        tmp_path,
        f"""
        import json
        import os
        import sqlite3

        os.environ["RECERTIA_EXECUTION_BACKEND"] = "local"
        from fastapi.testclient import TestClient
        from recertia.api import create_app

        root = pathlib.Path({str(root)!r})
        app = create_app(root=root)
        issued = app.state.api_keys.issue(tenant_id="t1", scopes={{"runs", "exec"}}, actor="test-admin")
        body = {{
            "run_id": "wheel-sync",
            "request": "write output.txt",
            "task_class": "repo-chore",
            "script": ["python3 -c \\"open('output.txt','w').write('done')\\""],
            "criteria": [
                {{
                    "id": "output-exists",
                    "kind": "command",
                    "run": "test -f output.txt",
                    "source": "caller",
                    "weight": 1.0,
                    "sensitivity_proof": {{
                        "criterion_id": "output-exists",
                        "negative_fixture": "empty workspace",
                        "rejected": True,
                        "checked_at": "2026-01-01T00:00:00Z",
                    }},
                }}
            ],
        }}
        with TestClient(app) as client:
            resp = client.post("/v1/runs", json=body, headers={{"X-API-Key": issued.secret}})
        payload = resp.json()
        db_candidates = list(root.rglob("runtime.db"))
        routes = []
        facts = 0
        for db in db_candidates:
            con = sqlite3.connect(db)
            try:
                routes = [json.loads(r[0]) for r in con.execute("select entry_json from route_entries")]
            except Exception:
                routes = []
            try:
                for _run_id, _seq, node, _next, state_json in con.execute(
                    "select run_id, seq, node, next_node, state_json from checkpoints"
                ):
                    if node == "distill":
                        st = json.loads(state_json)
                        facts = len(st.get("facts_extracted") or [])
            except Exception:
                pass
        distill_routes = [r for r in routes if r.get("node") == "distill"]
        print(
            json.dumps(
                {{
                    "status": resp.status_code,
                    "terminal": payload.get("terminal"),
                    "distill_routes": len(distill_routes),
                    "distill_route": distill_routes[0]["route"] if distill_routes else None,
                    "facts": facts,
                }}
            )
        )
        """,
    )
    result = json.loads(out.splitlines()[-1])
    assert result["status"] == 200, result
    assert result["terminal"] == "solved", result
    assert result["distill_routes"] >= 1, result
    assert result["facts"] >= 1, result
    assert "authoring_prior_missing" not in out, out
    print("api-distill-ok", result)


def test_authoring_prior_in_wheel_record_is_exactly_one_file(built_wheel: Path) -> None:
    names = sorted(
        path
        for path in _record(built_wheel)
        if path.endswith("authoring-prior.json") and not path.endswith("/")
    )
    assert names == ["recertia/authoring-prior.json"], names



def _run_installed_cli(
    installed: tuple[Path, Path], cwd: Path, args: list[str], env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    child, _ = installed
    script = child.parent / ("recertia.exe" if os.name == "nt" else "recertia")
    env = {
        k: v
        for k, v in _clean_env().items()
        if k not in {"RECERTIA_EXECUTION_BACKEND", "RECERTIA_CONTAINER_RUNTIME"}
    }
    env.update(env_extra or {})
    return subprocess.run(
        [str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_STEP_TIMEOUT_S,
        check=False,
    )


def _stub_container_runtime(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A ``docker`` on PATH that only records being called, so the container path gets past
    its runtime check without a real runtime. Bad input must stop before it is ever called."""

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    called = tmp_path / "stub-docker-called"
    if os.name == "nt":
        (bin_dir / "docker.bat").write_text(f'@echo called>>"{called}"\r\n@exit /b 1\r\n')
    else:
        stub = bin_dir / "docker"
        stub.write_text(f'#!/bin/sh\necho called >> "{called}"\nexit 1\n')
        stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "RECERTIA_EXECUTION_BACKEND": "container",
        "RECERTIA_CONTAINER_RUNTIME": "docker",
    }
    return env, called


@pytest.mark.parametrize(
    ("flag", "content", "named"),
    [
        ("--spec", "{not json", "spec bad.json is not valid JSON"),
        ("--goal", "{}", "goal bad.json is not a valid goal"),
    ],
    ids=["malformed-spec", "empty-goal"],
)
@pytest.mark.parametrize("backend", ["local-exec", "container-runtime-on-path"])
def test_cli_run_from_a_wheel_rejects_bad_input_with_invalid_config(
    installed_python: tuple[Path, Path], tmp_path: Path, flag: str, content: str, named: str, backend: str
) -> None:
    # Both arms reach input parsing: --local-exec, or the container backend with a (stub)
    # runtime on PATH. Each exits 2 with one invalid_config line naming only the file.
    _, child_site = installed_python
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    args = ["run", flag, str(bad), "--model", "stub", "--runs-root", str(tmp_path / "runs")]
    called = None
    if backend == "local-exec":
        proc = _run_installed_cli(installed_python, tmp_path, [*args, "--local-exec"])
    else:
        env_extra, called = _stub_container_runtime(tmp_path)
        proc = _run_installed_cli(installed_python, tmp_path, args, env_extra)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert proc.stderr.startswith(f"invalid_config: {named}"), output
    assert len(proc.stderr.strip().splitlines()) == 1, output
    assert "Traceback" not in output and 'File "' not in output, output
    venv_dir = child_site.parents[2] if os.name != "nt" else child_site.parents[1]
    for absolute in (str(venv_dir), str(child_site), str(tmp_path), "site-packages"):
        assert absolute not in output, (absolute, output)
    if called is not None:
        assert not called.exists(), "the container runtime was called before the input was checked"
