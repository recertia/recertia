"""The package version is declared in pyproject.toml and repeated as the API version."""

from __future__ import annotations

import tomllib
from pathlib import Path

from recertia.api import create_app

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _declared_version() -> str:
    with _PYPROJECT.open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_api_version_matches_the_package_version(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "root")
    assert app.version == _declared_version()
    assert app.openapi()["info"]["version"] == _declared_version()
