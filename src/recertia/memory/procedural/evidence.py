"""Read-only evidence port so memory does not import the eval store."""

from __future__ import annotations

from typing import Protocol

from contracts.eval import BinomialSample
from contracts.stats import RetrievalAblationEffect


class EvidenceSource(Protocol):
    """The slice of eval storage the active-set controller reads and writes."""

    def retrieval_ablation_samples(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> tuple[BinomialSample, BinomialSample]: ...

    def write_retrieval_ablation(self, effect: RetrievalAblationEffect) -> None: ...

    def contribution_samples_bulk(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> dict[tuple[str, int], tuple[BinomialSample, BinomialSample]]: ...
