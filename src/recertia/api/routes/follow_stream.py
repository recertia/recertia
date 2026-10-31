"""Async SSE body for ``GET /v1/runs/{run_id}/events?follow=1``.

A follow waits with ``asyncio.sleep`` and reads the log in short thread-pool calls, so an
open stream does not pin a worker thread. The concurrency slot is released when the
response ends for any reason, including a client that disconnects.

Some runs end without a terminal event in their log: the queue marks them ``dead`` (the
worker died on its last attempt), ``failed`` (``run_busy``, ``workspace_busy`` or a start-up
failure other than an ``OSError`` opening the run's persistence, which writes its own
terminal event), or ``cancelled`` or ``succeeded`` (the standalone worker writes no run
events). Once a follow has read everything in the log it asks ``ending`` whether the run
ended that way. If so it reads the log once more, because the worker may
have appended its own terminal event first: a real terminal event is sent instead of the
given frame, and otherwise the given frame is sent. Then it stops. The re-read also stops at
the max duration, without the given frame, so the client reconnects as usual.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from types import MappingProxyType
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from recertia.api.event_follow import KEEPALIVE_FRAME, EventFollower, FollowSettings

#: Sent on every SSE response so that caches and buffering proxies (nginx) pass frames on at once.
SSE_HEADERS: Mapping[str, str] = MappingProxyType({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


#: The ``terminal`` of the frame sent for each queue status that ends a run.
QUEUE_END_TERMINALS: Mapping[str, str] = MappingProxyType(
    {"dead": "error", "failed": "error", "cancelled": "cancelled", "succeeded": "succeeded"}
)


def ended_run_frame(tenant_id: str, run_id: str, status: str, error_code: str | None) -> str:
    """``run.finished`` for a run the queue ended (``status``) with no terminal event in its log.

    The payload is the queue's outcome: ``terminal`` is ``error`` for a ``dead`` or ``failed``
    row and the row's own status for a ``cancelled`` or ``succeeded`` row, and ``error_code``
    is the row's (JSON ``null`` when it has none). The event is not a line of the log, so the
    frame has no ``id:``: a client keeps the offset of the last logged event, and a reconnect
    from it ends with this frame again.
    """

    event = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "type": "run.finished",
        "payload": {"terminal": QUEUE_END_TERMINALS[status], "error_code": error_code},
    }
    return f"event: run.finished\ndata: {json.dumps(event)}\n\n"


async def aiter_follow(
    follower: EventFollower,
    settings: FollowSettings,
    *,
    ending: Callable[[], str | None] | None = None,
) -> AsyncIterator[str]:
    deadline = time.monotonic() + settings.max_seconds
    last_sent = time.monotonic()
    while True:
        frames, done, more = await run_in_threadpool(follower.poll)
        for frame in frames:
            yield frame
        now = time.monotonic()
        if frames:
            last_sent = now
        if done or now >= deadline:
            return
        if more:
            continue
        if ending is not None:
            final = await run_in_threadpool(ending)
            if final is not None:
                # An ended run has no live writer, so one more drain of the log is final.
                while True:
                    frames, done, more = await run_in_threadpool(follower.poll)
                    for frame in frames:
                        yield frame
                    if done or time.monotonic() >= deadline:
                        return
                    if not more:
                        break
                if follower.last_is_terminal:
                    return  # a real terminal event was sent; a torn line after it changes nothing
                yield final
                return
        if now - last_sent >= settings.keepalive_s:
            yield KEEPALIVE_FRAME
            last_sent = now
        await asyncio.sleep(min(settings.poll_s, max(0.0, deadline - now)))


class FollowResponse(StreamingResponse):
    """``text/event-stream`` response that gives its concurrency slot back when it ends."""

    def __init__(self, content: AsyncIterator[str], release: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, media_type="text/event-stream", headers=SSE_HEADERS, **kwargs)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()
