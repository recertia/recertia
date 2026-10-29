"""Follow mode for the run event log: byte-offset cursors, framing, and caps.

The SSE ``id:`` of a followed stream is the byte offset just past the event's log
line, so a client that reconnects with ``Last-Event-ID: <id>`` resumes exactly after
the last frame it saw. This module only needs the standard library so that
``recertia.api.events`` still imports without the optional FastAPI extra.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recertia.errors import ConfigError

#: Longest log line that is delivered. A longer line is skipped, never buffered whole.
MAX_LINE_BYTES = 1 << 20
#: Event types after which a run has nothing more to say (the follow ends at EOF).
TERMINAL_TYPES = frozenset({"run.finished", "run.cancelled"})
KEEPALIVE_FRAME = ": keep-alive\n\n"

_OFFSET_RE = re.compile(r"[0-9]{1,15}")
_EVENT_NAME_RE = re.compile(r"[^\x00-\x1f\x7f\x85\u2028\u2029]{1,128}")


class InvalidOffset(ValueError):
    """The client's resume cursor is not a position at the start of a logged event."""


class FollowCapExceeded(Exception):
    """A follow slot was refused; ``scope`` is ``"tenant"`` or ``"global"``."""

    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope


def parse_offset(raw: str) -> int:
    """Strict decimal byte offset: ASCII digits only, no sign, space, or underscore."""

    if _OFFSET_RE.fullmatch(raw) is None:
        raise InvalidOffset("Last-Event-ID must be a decimal byte offset")
    return int(raw)


def event_name(value: Any) -> str:
    """The SSE ``event:`` value; anything that could inject a field becomes ``unknown``."""

    if isinstance(value, str) and _EVENT_NAME_RE.fullmatch(value) is not None:
        return value
    return "unknown"


def sse_frame(event_id: str | int, event: Mapping[str, Any]) -> str:
    return f"id: {event_id}\nevent: {event_name(event.get('type'))}\ndata: {json.dumps(event)}\n\n"


def is_terminal(event: Mapping[str, Any]) -> bool:
    """Decided by the parsed event type, never by looking for text inside the line."""

    return event.get("type") in TERMINAL_TYPES


def _parse_line(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except ValueError:  # includes UnicodeDecodeError
        return None
    return event if isinstance(event, dict) else None


def check_offset(path: Path, offset: int) -> None:
    """Raise ``InvalidOffset`` unless ``offset`` is the start of a line inside the log."""

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if offset > size:
        raise InvalidOffset("Last-Event-ID is beyond the end of the run's event log")
    if offset == 0:
        return
    with path.open("rb") as f:
        f.seek(offset - 1)
        if f.read(1) != b"\n":
            raise InvalidOffset("Last-Event-ID is not the end of an event")


def offset_after_event_id(path: Path, event_id: str) -> int | None:
    """Byte offset just past the event with this id, or ``None`` when no event has it."""

    try:
        f = path.open("rb")
    except FileNotFoundError:
        return None
    with f:
        pos = 0
        oversize = False
        while True:
            raw = f.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return None
            pos += len(raw)
            complete = raw.endswith(b"\n")
            if oversize:
                oversize = not complete
                continue
            if not complete:
                if len(raw) <= MAX_LINE_BYTES:
                    return None  # a line still being written
                oversize = True
                continue
            event = _parse_line(raw)
            if event is not None and event.get("event_id") == event_id:
                return pos


def _previous_event_is_terminal(path: Path, offset: int) -> bool:
    start = max(0, offset - MAX_LINE_BYTES - 1)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(offset - start)
    line = data[:-1].rsplit(b"\n", 1)[-1] if data.endswith(b"\n") else b""
    event = _parse_line(line)
    return event is not None and is_terminal(event)


class EventFollower:
    """Reads newly appended complete events from a byte offset, one bounded pass at a time.

    ``offset`` is always the start of a line, except while an over-long line is being
    skipped. A line that is still being written (no ``\\n`` yet) is left alone and read
    again on the next pass, so no event is lost to a partial write.
    """

    def __init__(self, path: Path, offset: int = 0) -> None:
        self._path = path
        self.offset = offset
        self._skipping = False
        self._last_terminal = _previous_event_is_terminal(path, offset) if offset > 0 else False

    def poll(self) -> tuple[list[str], bool, bool]:
        """One pass. Returns ``(frames, done, more)``.

        ``done``: the last event is terminal and nothing follows it, or the log shrank
        below the cursor (truncated or replaced). ``more``: unread bytes remain and a
        further pass can make progress at once.
        """

        frames: list[str] = []
        try:
            f = self._path.open("rb")
        except FileNotFoundError:
            return frames, False, False
        with f:
            if os.fstat(f.fileno()).st_size < self.offset:
                return frames, True, False
            before = self.offset
            f.seek(self.offset)
            data = f.read(MAX_LINE_BYTES + 1)
            if data:
                self._consume(data, frames)
            size = os.fstat(f.fileno()).st_size
        progressed = self.offset != before
        return frames, self._last_terminal and self.offset == size, progressed and self.offset < size

    def _consume(self, data: bytes, frames: list[str]) -> None:
        if self._skipping:
            newline = data.find(b"\n")
            if newline < 0:
                self.offset += len(data)
            else:
                self.offset += newline + 1
                self._skipping = False
            return
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) > MAX_LINE_BYTES:
                self._skipping = True
                self.offset += len(data)
            return
        for line in data[:end].split(b"\n"):
            self.offset += len(line) + 1
            event = _parse_line(line)
            if event is None:
                continue
            frames.append(sse_frame(self.offset, event))
            self._last_terminal = is_terminal(event)


def iter_follow(
    follower: EventFollower,
    *,
    max_seconds: float,
    poll_s: float,
    keepalive_s: float,
) -> Iterator[str]:
    """Blocking iterator over SSE frames until the run is terminal or ``max_seconds`` pass."""

    deadline = time.monotonic() + max_seconds
    last_sent = time.monotonic()
    while True:
        frames, done, more = follower.poll()
        yield from frames
        now = time.monotonic()
        if frames:
            last_sent = now
        if done or now >= deadline:
            return
        if more:
            continue
        if now - last_sent >= keepalive_s:
            yield KEEPALIVE_FRAME
            last_sent = now
        time.sleep(min(poll_s, max(0.0, deadline - now)))


def _env_number(environ: Mapping[str, str], name: str, default: float, *, integer: bool) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a positive {'integer' if integer else 'number'}") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive {'integer' if integer else 'number'}")
    return value


@dataclass(frozen=True)
class FollowSettings:
    """Limits for ``follow=1`` streams. Every field has an environment override."""

    tenant_max: int = 16
    global_max: int = 64
    max_seconds: float = 30.0
    poll_s: float = 0.5
    keepalive_s: float = 15.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> FollowSettings:
        env = os.environ if environ is None else environ
        return cls(
            tenant_max=int(_env_number(env, "RECERTIA_TENANT_MAX_SSE", cls.tenant_max, integer=True)),
            global_max=int(_env_number(env, "RECERTIA_GLOBAL_MAX_SSE", cls.global_max, integer=True)),
            max_seconds=_env_number(env, "RECERTIA_SSE_MAX_S", cls.max_seconds, integer=False),
            poll_s=_env_number(env, "RECERTIA_SSE_POLL_S", cls.poll_s, integer=False),
            keepalive_s=_env_number(env, "RECERTIA_SSE_KEEPALIVE_S", cls.keepalive_s, integer=False),
        )


class FollowLimiter:
    """Counts open follow streams per tenant and overall; a refused slot is a 429."""

    def __init__(self, tenant_max: int, global_max: int) -> None:
        self._tenant_max = tenant_max
        self._global_max = global_max
        self._lock = threading.Lock()
        self._per_tenant: dict[str, int] = {}
        self._total = 0

    def acquire(self, tenant_id: str) -> Callable[[], None]:
        """Take a slot and return its release function (safe to call more than once)."""

        with self._lock:
            if self._total >= self._global_max:
                raise FollowCapExceeded("global")
            if self._per_tenant.get(tenant_id, 0) >= self._tenant_max:
                raise FollowCapExceeded("tenant")
            self._per_tenant[tenant_id] = self._per_tenant.get(tenant_id, 0) + 1
            self._total += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                left = self._per_tenant[tenant_id] - 1
                if left:
                    self._per_tenant[tenant_id] = left
                else:
                    del self._per_tenant[tenant_id]
                self._total -= 1

        return release

    def open_count(self, tenant_id: str | None = None) -> int:
        with self._lock:
            return self._total if tenant_id is None else self._per_tenant.get(tenant_id, 0)
