"""Async SSE body for ``GET /v1/runs/{run_id}/events?follow=1``.

A follow waits with ``asyncio.sleep`` and reads the log in short thread-pool calls, so an
open stream does not pin a worker thread. The concurrency slot is released when the
response ends for any reason, including a client that disconnects.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from recertia.api.event_follow import KEEPALIVE_FRAME, EventFollower, FollowSettings


async def aiter_follow(follower: EventFollower, settings: FollowSettings) -> AsyncIterator[str]:
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
        if now - last_sent >= settings.keepalive_s:
            yield KEEPALIVE_FRAME
            last_sent = now
        await asyncio.sleep(min(settings.poll_s, max(0.0, deadline - now)))


class FollowResponse(StreamingResponse):
    """``text/event-stream`` response that gives its concurrency slot back when it ends."""

    def __init__(self, content: AsyncIterator[str], release: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, media_type="text/event-stream", **kwargs)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()
