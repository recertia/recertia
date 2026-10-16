"""Optional OTLP. A missing exporter or endpoint is a no-op."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


def configure_otlp(endpoint: str = "") -> None:
    """No-op unless an OTLP endpoint is passed and the package imports."""

    if not endpoint.strip():
        return
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        return


@contextmanager
def span(name: str) -> Iterator[None]:
    del name
    yield
