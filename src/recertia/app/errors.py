"""Service-layer errors. HTTP mapping stays in the API adapters."""

from __future__ import annotations

from recertia.errors import RecertiaError


class ServiceError(RecertiaError):
    """Base for failures raised by ``recertia.app`` services."""
