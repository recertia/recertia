"""Practice-band proposals from one-offs and failure clusters."""

from __future__ import annotations

import json
from pathlib import Path

from recertia.jobs import Proposal


def load_one_off_reasons(one_off_log: Path | str | None) -> list[str]:
    """Cluster reasons from distill ``one_off_log`` JSONL (newest last, unique order)."""

    if one_off_log is None:
        return []
    path = Path(one_off_log)
    if not path.exists():
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        reason = str(row.get("reason") or "").strip()
        if not reason or reason in seen:
            continue
        seen.add(reason)
        ordered.append(reason)
    return ordered

def practice_from_one_offs(
    one_off_reasons: list[str],
    *,
    curriculum_dir: Path | None = None,
) -> list[Proposal]:
    """Build practice proposals in the predicted_success ∈ [0.2, 0.8] band.

    When ``curriculum_dir`` is set, write one curriculum JSON per cluster for the Practice job.
    """

    proposals: list[Proposal] = []
    if curriculum_dir is not None:
        curriculum_dir.mkdir(parents=True, exist_ok=True)
    for i, reason in enumerate(one_off_reasons):
        skill_id = f"practice-{i}"
        payload = {
            "predicted_success_band": [0.2, 0.8],
            "cluster_reason": reason,
            "excluded_from_user_metrics": True,
        }
        if curriculum_dir is not None:
            path = curriculum_dir / f"{skill_id}.json"
            path.write_text(
                json.dumps(
                    {
                        "skill_id": skill_id,
                        "predicted_success": 0.5,
                        "band": [0.2, 0.8],
                        "reason": reason,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            payload["curriculum_path"] = str(path)
        proposals.append(
            Proposal(
                kind="practice",
                skill_id=skill_id,
                version=1,
                rationale=f"practice curriculum from one-off cluster: {reason[:60]}",
                payload=payload,
            )
        )
    return proposals

def practice_from_fail_clusters(
    eligible_rows: list,
    *,
    curriculum_dir: Path | None = None,
) -> list[Proposal]:
    """Prefer incremental eligible clusters over random one-offs (ADR-0015 P2)."""

    proposals: list[Proposal] = []
    if curriculum_dir is not None:
        curriculum_dir.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(eligible_rows):
        signature = getattr(row, "signature", None) or str(row)
        task_class = getattr(row, "task_class", "unknown")
        skill_id = f"practice-cluster-{i}"
        payload = {
            "predicted_success_band": [0.2, 0.8],
            "cluster_signature": signature,
            "task_class": task_class,
            "excluded_from_user_metrics": True,
            "source": "fail_cluster",
        }
        if curriculum_dir is not None:
            path = curriculum_dir / f"{skill_id}.json"
            path.write_text(
                json.dumps(
                    {
                        "skill_id": skill_id,
                        "predicted_success": 0.5,
                        "band": [0.2, 0.8],
                        "signature": signature,
                        "task_class": task_class,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            payload["curriculum_path"] = str(path)
        proposals.append(
            Proposal(
                kind="fail_cluster",
                skill_id=skill_id,
                version=1,
                rationale=f"fail-cluster curriculum: {signature[:60]}",
                payload=payload,
            )
        )
    return proposals
