"""Load the versioned authoring prior (T2)."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from contracts.policy import AuthoringPrior
from recertia.errors import RecertiaError

# Packaged default (wheels ship this under recertia/); no repo-root parents[] lookup.
_PACKAGED_NAME = "authoring-prior.json"


class AuthoringPriorMissing(RecertiaError):
    """The authoring prior file is missing (the message never carries an absolute path)."""

    code = "authoring_prior_missing"
    http_status = 500
    retryable = False
    exit_code = 7


def _packaged_prior_text() -> str:
    resource = files("recertia").joinpath(_PACKAGED_NAME)
    if not resource.is_file():
        raise AuthoringPriorMissing(
            f"default authoring prior {_PACKAGED_NAME} not found"
        )
    return resource.read_text(encoding="utf-8")


@lru_cache(maxsize=4)
def load_authoring_prior(path: str | None = None) -> AuthoringPrior:
    if path:
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise AuthoringPriorMissing(f"authoring prior {target.name} not found") from None
        return AuthoringPrior.model_validate(json.loads(text))
    try:
        text = _packaged_prior_text()
    except AuthoringPriorMissing:
        raise
    except FileNotFoundError:
        raise AuthoringPriorMissing(
            f"default authoring prior {_PACKAGED_NAME} not found"
        ) from None
    return AuthoringPrior.model_validate(json.loads(text))
