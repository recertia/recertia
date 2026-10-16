"""Classify provider failures, jitter retries, and open a per-model circuit."""

from __future__ import annotations

import random
import threading
import time
from enum import StrEnum

from recertia.errors import RecertiaError
from recertia.observability.metrics import metrics


class ModelUnavailable(RecertiaError):
    """The provider cannot be called now. ``details.reason`` says why."""

    code = "execution_unavailable"
    http_status = 503
    retryable = True
    exit_code = 7


class Outcome(StrEnum):
    OK = "ok"
    CLIENT_ERROR = "client_error"
    RETRYABLE = "retryable"
    TIMEOUT = "timeout"


_CLIENT_STATUS = frozenset({400, 401, 403, 404, 409, 422})
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def classify(exc: BaseException) -> Outcome:
    """Retry only failures that can succeed later. Walk ``__cause__`` one step."""

    current: BaseException | None = exc
    seen = 0
    while current is not None and seen < 6:
        status = getattr(current, "status", None)
        if isinstance(status, int):
            if status in _CLIENT_STATUS:
                return Outcome.CLIENT_ERROR
            if status in _RETRY_STATUS:
                return Outcome.RETRYABLE
            return Outcome.CLIENT_ERROR
        if isinstance(current, TimeoutError):
            return Outcome.TIMEOUT
        if isinstance(current, (ConnectionResetError, ConnectionError)):
            return Outcome.RETRYABLE
        names = {cls.__name__ for cls in type(current).__mro__}
        if "URLError" in names or "IncompleteRead" in names:
            return Outcome.RETRYABLE
        current = current.__cause__
        seen += 1
    return Outcome.CLIENT_ERROR


def backoff_s(
    attempt: int,
    *,
    base_s: float,
    cap_s: float,
    retry_after_s: float | None,
    rng: random.Random | None = None,
) -> float:
    """Full jitter in ``(0, min(cap, base * 2**attempt)]``.

    A ``Retry-After`` delay is a lower bound, still capped.
    """

    ceiling = min(cap_s, base_s * (2**attempt))
    if ceiling < 0:
        ceiling = 0.0
    roll = (rng or random).uniform(0.0, 1.0)
    if roll == 0.0:
        roll = 1.0
    wait = roll * ceiling
    if retry_after_s is not None and retry_after_s > 0:
        wait = max(wait, retry_after_s)
    return min(cap_s, wait)


class _CircuitState:
    def __init__(self) -> None:
        self.phase = "closed"
        self.failures: list[float] = []
        self.opened_at = 0.0
        self.probe = False


class CircuitBreaker:
    """Process-local breaker keyed by ``(provider, model)``."""

    def __init__(
        self,
        *,
        threshold: int = 5,
        window_s: float = 30.0,
        open_s: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.window_s = window_s
        self.open_s = open_s
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], _CircuitState] = {}

    def reset(self) -> None:
        with self._lock:
            self._states.clear()

    def _state(self, key: tuple[str, str]) -> _CircuitState:
        state = self._states.get(key)
        if state is None:
            state = _CircuitState()
            self._states[key] = state
        return state

    def _gauge(self, key: tuple[str, str], value: float) -> None:
        metrics.set_gauge(
            "recertia_circuit_state",
            value,
            provider=key[0] or "unknown",
            model=key[1] or "unknown",
        )

    def before_call(self, key: tuple[str, str]) -> None:
        with self._lock:
            state = self._state(key)
            now = self._clock()
            if state.phase == "open" and (now - state.opened_at) >= self.open_s:
                state.phase = "half_open"
                state.probe = False
            if state.phase == "open":
                self._gauge(key, 1)
                raise ModelUnavailable(
                    f"circuit open for {key[0]}/{key[1]}",
                    details={"reason": "circuit_open"},
                )
            if state.phase == "half_open":
                if state.probe:
                    raise ModelUnavailable(
                        f"circuit half-open for {key[0]}/{key[1]}",
                        details={"reason": "circuit_open"},
                    )
                state.probe = True
                self._gauge(key, 2)
                return
            self._gauge(key, 0)

    def record_success(self, key: tuple[str, str]) -> None:
        with self._lock:
            state = self._state(key)
            state.phase = "closed"
            state.failures.clear()
            state.probe = False
            self._gauge(key, 0)

    def record_failure(self, key: tuple[str, str]) -> None:
        with self._lock:
            state = self._state(key)
            now = self._clock()
            if state.phase == "half_open":
                state.phase = "open"
                state.opened_at = now
                state.probe = False
                self._gauge(key, 1)
                return
            state.failures = [stamp for stamp in state.failures if now - stamp <= self.window_s]
            state.failures.append(now)
            if len(state.failures) >= self.threshold:
                state.phase = "open"
                state.opened_at = now
                self._gauge(key, 1)


_DEFAULT: CircuitBreaker | None = None
_DEFAULT_LOCK = threading.Lock()


def default_breaker() -> CircuitBreaker:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            from recertia.settings import load_settings

            settings = load_settings()
            _DEFAULT = CircuitBreaker(
                threshold=settings.circuit_failures,
                window_s=settings.circuit_window_s,
                open_s=settings.circuit_open_s,
            )
        return _DEFAULT
