"""A missing authoring prior raises a named error whose message has no absolute path."""

from __future__ import annotations

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


def test_missing_default_prior_names_it_without_an_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _missing() -> str:
        raise prior.AuthoringPriorMissing(
            "default authoring prior authoring-prior.json not found"
        ) from None

    monkeypatch.setattr(prior, "_packaged_prior_text", _missing)
    with pytest.raises(prior.AuthoringPriorMissing) as caught:
        prior.load_authoring_prior()
    message = str(caught.value)
    assert "authoring-prior.json" in message
    assert "default authoring prior" in message
    assert str(tmp_path) not in message
    assert "policy/" not in message
    assert isinstance(caught.value, RecertiaError)
    assert caught.value.code == "authoring_prior_missing"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_missing_explicit_prior_names_the_file_only(tmp_path: Path) -> None:
    with pytest.raises(prior.AuthoringPriorMissing) as caught:
        prior.load_authoring_prior(str(tmp_path / "custom-prior.json"))
    assert str(caught.value) == "authoring prior custom-prior.json not found"


def test_default_prior_still_loads_from_the_packaged_resource() -> None:
    loaded = prior.load_authoring_prior()
    assert loaded.version == "ap-2026.08.1"
    assert loaded.max_steps == 12


def test_explicit_valid_prior_still_loads(tmp_path: Path) -> None:
    packaged = Path(prior.__file__).resolve().parents[1] / "authoring-prior.json"
    target = tmp_path / "custom-prior.json"
    target.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
    loaded = prior.load_authoring_prior(str(target))
    assert loaded.version == "ap-2026.08.1"
