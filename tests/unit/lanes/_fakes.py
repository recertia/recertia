"""Shared fakes for provider-lane tests (no network)."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from typing import Any


class FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def ok(payload: Any) -> FakeResp:
    return FakeResp(json.dumps(payload).encode())


def http_error(code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://example.invalid/v1", code, "err", hdrs, io.BytesIO(body))


class Script:
    """Callable for patch('urllib.request.urlopen', side_effect=...): returns/raises in order."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.requests: list[Any] = []

    def __call__(self, req: Any, timeout: float | None = None) -> Any:
        self.requests.append((req, timeout))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step
