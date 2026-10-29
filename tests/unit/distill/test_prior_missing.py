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
    monkeypatch.setattr(prior, "_DEFAULT", tmp_path / "policy" / "authoring-prior.json")
    with pytest.raises(prior.AuthoringPriorMissing) as caught:
        prior.load_authoring_prior()
    message = str(caught.value)
    assert "policy/authoring-prior.json" in message
    assert "source checkout" in message
    assert str(tmp_path) not in message
    assert isinstance(caught.value, RecertiaError)
    assert caught.value.code == "authoring_prior_missing"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_missing_explicit_prior_names_the_file_only(tmp_path: Path) -> None:
    with pytest.raises(prior.AuthoringPriorMissing) as caught:
        prior.load_authoring_prior(str(tmp_path / "custom-prior.json"))
    assert str(caught.value) == "authoring prior custom-prior.json not found"


def test_default_prior_still_loads_from_a_source_checkout() -> None:
    assert prior.load_authoring_prior().version
