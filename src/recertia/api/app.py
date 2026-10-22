"""ASGI entrypoint: ``uvicorn recertia.api.app:app --no-proxy-headers``.

Keep ``--no-proxy-headers``: uvicorn's default rewrites the client address from
X-Forwarded-For for loopback peers, which would bypass ``RECERTIA_TRUSTED_PROXIES``.

The app object is created on first attribute access, not at import time, so
``import recertia.api.app`` does not mkdir ``.recertia/`` in the current
working directory.
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "app":
        from recertia.api import create_app

        app = create_app()
        globals()["app"] = app
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
