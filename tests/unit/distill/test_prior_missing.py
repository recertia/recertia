"""A missing authoring prior raises a named error whose message has no absolute path."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from recertia.distill import prior
from recertia.errors import RecertiaError


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    prior.load_authoring_prior.cache_clear()
    yield
    prior.load_authoring_prior.cache_clear()


class _MissingTraversable:
    """Minimal Traversable stand-in: joinpath works, is_file is always false."""

    def joinpath(self, *parts: str) -> "_MissingTraversable":
        return self

    def is_file(self) -> bool:
        return False

    def read_text(self, encoding: str = "utf-8") -> str:  # pragma: no cover - not reached
        raise FileNotFoundError("authoring-prior.json")


def test_missing_default_prior_names_it_without_an_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Point package resource lookup at a Traversable without the file so the real
    # resolver path in _packaged_prior_text raises AuthoringPriorMissing.
    monkeypatch.setattr(prior, "files", lambda _name: _MissingTraversable())
    with pytest.raises(prior.AuthoringPriorMissing) as caught:
        prior.load_authoring_prior()
    message = str(caught.value)
    assert "authoring-prior.json" in message
    assert "default authoring prior" in message
    assert str(tmp_path) not in message
    assert "policy/" not in message
    assert isinstance(caught.value, RecertiaError)
    assert caught.value.code == "authoring_prior_missing"
    assert caught.value.__cause__ is None


def test_missing_explicit_prior_names_the_file_only(tmp_path: Path) -> None:
    with pytest.raises(prior.AuthoringPriorMissing) as caught:
        prior.load_authoring_prior(str(tmp_path / "custom-prior.json"))
    assert str(caught.value) == "authoring prior custom-prior.json not found"


def test_default_prior_still_loads_from_the_packaged_resource() -> None:
    loaded = prior.load_authoring_prior()
    assert loaded.version == "ap-2026.08.1"
    assert loaded.max_steps == 12


def test_explicit_valid_prior_still_loads(tmp_path: Path) -> None:
    # Bytes differ from the packaged default (custom version) so a silent fallback
    # to the packaged file would fail the assertion.
    custom = {
        "schema_version": "1.0",
        "version": "ap-custom-test-9",
        "require_parameter_or_recurrence": True,
        "require_non_judge_criterion": True,
        "require_sensitivity_proof": True,
        "require_failure_modes": True,
        "max_steps": 7,
        "prefer_shell_when_applicable": True,
        "notes": ["explicit custom prior for boundary proof"],
    }
    target = tmp_path / "custom-prior.json"
    target.write_text(json.dumps(custom) + "\n", encoding="utf-8")
    loaded = prior.load_authoring_prior(str(target))
    assert loaded.version == "ap-custom-test-9"
    assert loaded.max_steps == 7
    assert loaded.version != "ap-2026.08.1"
