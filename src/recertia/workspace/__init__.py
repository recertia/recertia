"""Per-attempt workspace isolation (specs §17, M0)."""

from recertia.workspace.offload import OffloadError, OffloadHandle, WorkingSetOffload
from recertia.workspace.snapshot import WorkspaceManager

__all__ = ["OffloadError", "OffloadHandle", "WorkspaceManager", "WorkingSetOffload"]
