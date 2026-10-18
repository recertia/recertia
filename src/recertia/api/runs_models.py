"""Compatibility re-export. New code imports ``recertia.api.schemas``."""

from recertia.api.schemas import RunCreate, RunRecord

__all__ = ["RunCreate", "RunRecord"]
