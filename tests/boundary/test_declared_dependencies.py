"""Every third-party import in src/recertia must be a declared core or extra dependency.

Optional guarded imports (pypdf, opentelemetry) map to extras rather than core.
starlette is pulled in through FastAPI and maps to the ``api`` extra.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "recertia"
PYPROJECT = REPO_ROOT / "pyproject.toml"

FIRST_PARTY = frozenset({"recertia", "contracts"})

# Import root → extra name. None means a core [project.dependencies] package.
DECLARED_IMPORTS: dict[str, str | None] = {
    "pydantic": None,
    "typer": None,
    "jsonschema": None,
    "fastapi": "api",
    "starlette": "api",
    "uvicorn": "api",
    "httpx": "api",
    "pypdf": "pdf",
    "opentelemetry": "otel",
    "psycopg": "postgres",
}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _core_requirement_names(data: dict) -> set[str]:
    names: set[str] = set()
    for req in data["project"]["dependencies"]:
        names.add(_req_name(req))
    return names


def _extra_requirement_names(data: dict) -> dict[str, set[str]]:
    extras: dict[str, set[str]] = {}
    for extra, reqs in data["project"].get("optional-dependencies", {}).items():
        extras[extra] = {_req_name(req) for req in reqs}
    return extras


def _req_name(req: str) -> str:
    for sep in ("[", ">", "<", "=", "!", "~", ";"):
        req = req.split(sep, 1)[0]
    return req.strip().lower()


def _is_type_checking_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _import_roots(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    guarded: set[ast.AST] = set()
    for node in ast.walk(tree):
        if _is_type_checking_guard(node):
            for child in ast.walk(node):
                if child is not node:
                    guarded.add(child)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if node in guarded:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            if node.module:
                roots.add(node.module.split(".", 1)[0])
    roots.discard("__future__")
    return roots


def _iter_src_py() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if p.is_file())


def test_starlette_is_mapped_to_the_api_extra() -> None:
    assert DECLARED_IMPORTS["starlette"] == "api"
    data = _pyproject()
    extras = _extra_requirement_names(data)
    assert "api" in extras
    assert "fastapi" in extras["api"]


def test_jsonschema_is_a_core_dependency() -> None:
    data = _pyproject()
    assert "jsonschema" in _core_requirement_names(data)
    extras = _extra_requirement_names(data)
    assert "jsonschema" not in extras.get("dev", set())


def test_pdf_and_otel_are_optional_extras() -> None:
    data = _pyproject()
    extras = _extra_requirement_names(data)
    assert "pypdf" in extras["pdf"]
    assert "opentelemetry-api" in extras["otel"]
    core = _core_requirement_names(data)
    assert "pypdf" not in core
    assert "opentelemetry-api" not in core


def test_every_third_party_import_is_declared() -> None:
    stdlib = set(sys.stdlib_module_names)
    data = _pyproject()
    extras = _extra_requirement_names(data)
    undeclared: list[str] = []
    extra_mismatch: list[str] = []
    for source in _iter_src_py():
        rel = source.relative_to(REPO_ROOT)
        for root in sorted(_import_roots(source)):
            if root in stdlib or root in FIRST_PARTY:
                continue
            if root not in DECLARED_IMPORTS:
                undeclared.append(f"{rel}: {root}")
                continue
            extra = DECLARED_IMPORTS[root]
            if extra is None:
                continue
            if extra not in extras:
                extra_mismatch.append(f"{rel}: {root} maps to missing extra {extra!r}")
    assert not undeclared, "undeclared third-party imports:\n" + "\n".join(undeclared)
    assert not extra_mismatch, "extra mapping errors:\n" + "\n".join(extra_mismatch)


def test_packaged_policy_matches_repo_default() -> None:
    from importlib.resources import files

    packaged = files("recertia").joinpath("policy_default.json").read_text(encoding="utf-8")
    repo = (REPO_ROOT / "policy" / "default.json").read_text(encoding="utf-8")
    assert packaged == repo


def test_pyproject_ships_store_migrations() -> None:
    data = _pyproject()
    package_data = data["tool"]["setuptools"]["package-data"]
    assert "migrations/*.sql" in package_data["recertia.store"]
    from recertia.store import list_migrations

    assert list_migrations(dialect="sqlite"), "sqlite migrations must be present"
    assert list_migrations(dialect="postgres"), "postgres migrations must be present"
