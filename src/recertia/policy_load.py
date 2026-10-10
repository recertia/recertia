"""Load the T2 Policy document and the T0 weekly JobQuota sidecar (P0″)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from contracts.policy import JobQuota, Policy

_PACKAGED_POLICY = "policy_default.json"


def _packaged_policy_text() -> str:
    resource = files("recertia").joinpath(_PACKAGED_POLICY)
    if not resource.is_file():
        raise FileNotFoundError(
            "packaged default policy is missing from the install; "
            "set RECERTIA_POLICY_PATH or reinstall recertia with package data"
        )
    return resource.read_text(encoding="utf-8")


def default_policy_path() -> Path:
    """Return the override path, or the packaged default when it is on disk."""

    override = os.environ.get("RECERTIA_POLICY_PATH", "").strip()
    if override:
        return Path(override)
    resource = files("recertia").joinpath(_PACKAGED_POLICY)
    if not resource.is_file():
        raise FileNotFoundError(
            "packaged default policy is missing from the install; "
            "set RECERTIA_POLICY_PATH or reinstall recertia with package data"
        )
    return Path(str(resource))


def load_policy(path: Path | str | None = None) -> Policy:
    """Read the versioned Policy document. Missing path raises; do not invent flags.

    Default policy is the copy shipped in the wheel (``importlib.resources``), not a
    repo-relative ``parents[2]`` path. ``RECERTIA_POLICY_PATH`` still overrides.
    """

    if path is not None:
        target = Path(path)
        return Policy.model_validate_json(target.read_text(encoding="utf-8"))
    override = os.environ.get("RECERTIA_POLICY_PATH", "").strip()
    if override:
        return Policy.model_validate_json(Path(override).read_text(encoding="utf-8"))
    return Policy.model_validate_json(_packaged_policy_text())


def iso_week_id(at: datetime | None = None) -> str:
    when = at or datetime.now(timezone.utc)
    return when.strftime("%G-W%V")


class QuotaSidecar:
    """Week-scoped spend. Rebuildable; never rewrite ``policy/*.json``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def merge(self, base: JobQuota, *, at: datetime | None = None) -> JobQuota:
        week = iso_week_id(at)
        data = self._read()
        if not data or data.get("week_id") != week:
            return base.model_copy(
                update={
                    "tokens_spent": 0,
                    "hex_tokens_spent": 0,
                    "hex_jobs_by_class": {},
                }
            )
        by_class = data.get("hex_jobs_by_class") or {}
        if not isinstance(by_class, dict):
            by_class = {}
        return base.model_copy(
            update={
                "tokens_spent": int(data.get("tokens_spent") or 0),
                "hex_tokens_spent": int(data.get("hex_tokens_spent") or 0),
                "hex_jobs_by_class": {str(k): int(v) for k, v in by_class.items()},
            }
        )

    def save(self, quota: JobQuota, *, at: datetime | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "week_id": iso_week_id(at),
            "tokens_spent": quota.tokens_spent,
            "hex_tokens_spent": quota.hex_tokens_spent,
            "hex_jobs_by_class": quota.hex_jobs_by_class,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}
