"""Shared/exclusive lock for one runtime root, and per-tenant locks under it.

The API holds a shared lock for its lifetime. Backup and restore take an
exclusive lock and refuse when the shared lock is already held.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from recertia.errors import RecertiaError

_IS_WINDOWS = sys.platform == "win32"

class RootLockBusy(RuntimeError):
    """An exclusive lock could not be taken because the root is in use."""

class RootBusy(RootLockBusy, RecertiaError):
    """Typed busy error mapped to the spec: code root_busy, 503, retryable, exit 6."""

    code = "root_busy"
    http_status = 503
    retryable = True
    exit_code = 6

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message)
        self.message = message
        self.run_id = kwargs.get("run_id")
        self.details = dict(kwargs.get("details") or {})
        self.retry_after_s = kwargs.get("retry_after_s")
