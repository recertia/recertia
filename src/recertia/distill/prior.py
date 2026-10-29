"""Load the versioned authoring prior (T2)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from contracts.policy import AuthoringPrior
from recertia.errors import RecertiaError

# The default prior lives in the repository's policy/ directory, which wheels do not ship.
_DEFAULT_NAME = "policy/authoring-prior.json"
_DEFAULT = Path(__file__).resolve().parents[3] / _DEFAULT_NAME


class AuthoringPriorMissing(RecertiaError):
    """The authoring prior file is missing (the message never carries an absolute path)."""

    code = "authoring_prior_missing"
    http_status = 500
    retryable = False
    exit_code = 7


@lru_cache(maxsize=4)
def load_authoring_prior(path: str | None = None) -> AuthoringPrior:
    target = Path(path) if path else _DEFAULT
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path:
            message = f"authoring prior {target.name} not found"
        else:
            message = (
                f"default authoring prior {_DEFAULT_NAME} not found: wheel installs do not "
                "include it; run from a source checkout"
            )
        raise AuthoringPriorMissing(message) from None
    return AuthoringPrior.model_validate(json.loads(text))
