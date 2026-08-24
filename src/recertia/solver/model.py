"""Model client with retry, timeout, token and cost accounting (M2).

Production deployments inject a real provider client. Tests and offline runs use
:class:`StubModelClient`, which returns scripted responses and still exercises the same
retry/timeout/spend path the real client uses — so replay tests reconstruct decisions
with no model calls (M2 done-when).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    model: str = "stub"
    latency_s: float = 0.0


@dataclass
class ModelSpend:
    calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    retries: int = 0


class ModelClient(ABC):
    def __init__(
        self,
        *,
        max_retries: int = 2,
        timeout_s: float = 60.0,
        retry_backoff_s: float = 0.05,
        provider: str | None = None,
        model_id: str | None = None,
        role: str = "solver",
        credential_id: str | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.retry_backoff_s = retry_backoff_s
        self.provider = provider
        self.model_id = model_id
        self.role = role
        self.credential_id = credential_id
        self.spend = ModelSpend()

    def model_identity(self) -> tuple[str, str, str] | None:
        """Return a comparable provider identity when configured.

        Clients without explicit provider/model metadata fall back to object identity
        checks only — production verifier wiring should always set these fields.
        """

        if self.provider is None or self.model_id is None:
            return None
        return (self.provider, self.model_id, self.credential_id or "")

    def shares_identity_with(self, other: "ModelClient") -> bool:
        if self is other:
            return True
        left = self.model_identity()
        right = other.model_identity()
        if left is None or right is None:
            return False
        return left == right

    def complete(self, prompt: str, *, system: str | None = None) -> ModelResponse:
        """Call the provider with retry/timeout; accumulate spend on success."""

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                response = self._complete_once(prompt, system=system)
                response.latency_s = time.monotonic() - started
                if response.latency_s > self.timeout_s:
                    raise TimeoutError(
                        f"model call exceeded timeout_s={self.timeout_s} "
                        f"(took {response.latency_s:.2f}s)"
                    )
                self.spend.calls += 1
                self.spend.tokens += response.prompt_tokens + response.completion_tokens
                self.spend.cost_usd += response.cost_usd
                if attempt > 0:
                    self.spend.retries += attempt
                from recertia.telemetry import emit_in_run

                emit_in_run(
                    "model.completed",
                    role=self.role,
                    model=response.model,
                    tokens=response.prompt_tokens + response.completion_tokens,
                    latency_ms=round(response.latency_s * 1000.0, 3),
                    retries=attempt,
                )
                return response
            except Exception as exc:  # noqa: BLE001 — retryable boundary
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff_s * (2**attempt))
        assert last_exc is not None
        raise last_exc

    @abstractmethod
    def _complete_once(self, prompt: str, *, system: str | None) -> ModelResponse:
        ...


class StubModelClient(ModelClient):
    """Scripted model: a queue of responses, or a callable mapper from prompt → text.

    Used by replay tests and by ``scratch`` solving when no provider is configured.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        mapper: Callable[[str], str] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._queue = list(responses or [])
        self._mapper = mapper

    def _complete_once(self, prompt: str, *, system: str | None) -> ModelResponse:
        if self._queue:
            text = self._queue.pop(0)
        elif self._mapper is not None:
            text = self._mapper(prompt)
        else:
            text = "true  # stub model: no-op"
        return ModelResponse(
            text=text,
            prompt_tokens=max(1, len(prompt) // 4),
            completion_tokens=max(1, len(text) // 4),
            cost_usd=0.0,
            model="stub",
        )


@dataclass
class ModelCallRecord:
    """One recorded call, for the structured transcript."""

    prompt_excerpt: str
    response_excerpt: str
    model: str
    tokens: int
    cost_usd: float
    latency_s: float
    system: str | None = None
    extras: dict = field(default_factory=dict)
