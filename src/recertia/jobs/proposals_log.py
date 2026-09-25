"""JSONL sidecar for curator proposals so specificity review survives process restarts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from recertia.jobs import Proposal, ProposalKind


class ProposalLog:
    """Append-only proposal log. Used to skip already-flagged specificity reviews."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> list[Proposal]:
        if not self.path.is_file():
            return []
        out: list[Proposal] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            created = row.get("created_at")
            created_at = (
                datetime.fromisoformat(created) if isinstance(created, str) else datetime.now(timezone.utc)
            )
            kind: ProposalKind = row.get("kind") or "curate"
            out.append(
                Proposal(
                    kind=kind,
                    skill_id=str(row.get("skill_id") or ""),
                    version=int(row.get("version") or 0),
                    rationale=str(row.get("rationale") or ""),
                    payload=dict(row.get("payload") or {}),
                    created_at=created_at,
                )
            )
        return out

    def append(self, proposals: list[Proposal]) -> None:
        if not proposals:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for proposal in proposals:
                handle.write(
                    json.dumps(
                        {
                            "kind": proposal.kind,
                            "skill_id": proposal.skill_id,
                            "version": proposal.version,
                            "rationale": proposal.rationale,
                            "payload": proposal.payload,
                            "created_at": proposal.created_at.isoformat(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
