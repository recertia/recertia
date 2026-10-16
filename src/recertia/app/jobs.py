"""One job-name table for the CLI and the HTTP API."""

from __future__ import annotations

# Alias → canonical name. Both dispatchers branch on the canonical name.
_CANONICAL: dict[str, str] = {
    "mine": "mine",
    "miner": "mine",
    "curator": "curator",
    "curate": "curator",
    "practice": "practice",
    "recertify": "recertify",
    "recertifier": "recertify",
    "shadow": "shadow",
    "parallelise": "parallelise",
    "parallelize": "parallelise",
    "serialise": "serialise",
    "serialize": "serialise",
    "correction": "correction",
    "correction_miner": "correction",
    "hex": "hex",
    "practice_hex": "hex",
    "compress": "compress",
}


def canonical_job(name: str) -> str | None:
    """Return the shared job id, or None when the name is not registered."""

    return _CANONICAL.get(name.strip().lower())


def known_job_names() -> tuple[str, ...]:
    return tuple(sorted(set(_CANONICAL.values())))
