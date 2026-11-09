"""Phase C production caller. Scores, console, recalibration, index, rollback.

Calls ``recertia.rsi``. Does not solve, does not write an active skill, and
does not set ``RECERTIA_HYGIENE_ENABLED``, ``RECERTIA_CALIBRATED_SCORING``, or
``RECERTIA_PHASE4_RETIREMENT``. Retirement reads the legacy score.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from recertia.ledger.hashchain import HashChainLedger
from recertia.memory.episodic import EpisodicStore
from recertia.rsi.attribution import attribute
from recertia.rsi.models import (
    CalibConstants,
    RecalibrationManifest,
    ScoreSidecar,
    SkillLineage,
    SkillVersion,
    TurnView,
)
from recertia.rsi.pipeline import RetrievalIndex, run_recalibration, snapshot_views
from recertia.rsi.scoring import accept_constants, contribution_for_retirement, score_run
from recertia.rsi.world import World

DEFAULT_GAMMA_TURN = 0.95
_ACTOR = "rsi-phase-c"


def golden_observations(repo_root: Path) -> list[dict[str, Any]]:
    """Promotion logs under the golden tree. Not the synthetic 30-row script."""

    log_dir = Path(repo_root) / "evals" / "golden" / "_promotion_logs"
    rows: list[dict[str, Any]] = []
    if not log_dir.is_dir():
        return rows
    for path in sorted(log_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not doc.get("run_id"):
            continue
        strict = doc.get("passed") is True and doc.get("terminal") == "solved"
        rows.append(
            {
                "run_id": str(doc["run_id"]),
                "task_class": _task_class(doc),
                "checks_passed": 1 if strict else 0,
                "checks_total": 1,
                "strict_success": strict,
            }
        )
    return rows


def golden_constants(observations: list[dict[str, Any]]) -> CalibConstants:
    """Quantiles from the golden logs. ``approved_by`` stays empty."""

    quantiles: dict[str, float] = {}
    for row in observations:
        task = str(row["task_class"])
        passed = int(row["checks_passed"])
        if passed > 0:
            quantiles[task] = max(quantiles.get(task, 0.0), float(passed))
    if not quantiles:
        quantiles["unset"] = 1.0
    return CalibConstants(
        version="calib_golden20261109",
        family_quantile=quantiles,
        approved_by="",
        approved_at="",
        partial_scale=0.6,
        fallback_d=82.0,
    )


def golden_report(repo_root: Path) -> dict[str, Any]:
    """Candidate constants from the golden set. Never activated, never installed."""

    observations = golden_observations(repo_root)
    constants = golden_constants(observations)
    scored = [
        score_run(
            str(row["run_id"]),
            str(row["task_class"]),
            int(row["checks_passed"]),
            int(row["checks_total"]),
            bool(row["strict_success"]),
            constants,
        )
        for row in observations
    ]
    if scored:
        accepted, detail = accept_constants(scored)
    else:
        accepted, detail = False, {"reason": "empty corpus"}
    return {
        "activated": False,
        "corpus": "golden",
        "not_for_activation": True,
        "accepted": accepted,
        "detail": detail,
        "records": len(scored),
        "constants": {
            "version": constants.version,
            "family_quantile": constants.family_quantile,
            "partial_scale": constants.partial_scale,
            "fallback_d": constants.fallback_d,
            "approved_by": "",
        },
    }


def write_score_sidecar(ledger: HashChainLedger, row: ScoreSidecar) -> str:
    """One sidecar row per run. A replay of the same run does not append again."""

    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if evidence.get("kind") == "rsi_score_sidecar" and evidence.get("run_id") == row.run_id:
            return str(entry.entry_hash)
    entry = ledger.append(
        actor=_ACTOR,
        action="write",
        target=row.run_id,
        evidence={
            "kind": "rsi_score_sidecar",
            "run_id": row.run_id,
            "legacy": row.legacy,
            "dense": row.dense,
            "prioritized": row.prioritized,
            "constants_version": row.constants_version,
            "strict_success": row.strict_success,
            "task_class": row.task_class,
        },
    )
    return str(entry.entry_hash)


def retirement_mass(row: ScoreSidecar) -> float:
    """Legacy check mass. Phase 4 is not callable from this module."""

    return contribution_for_retirement(row, phase4=False)


def kept_by_retirement(rows: list[ScoreSidecar], threshold: float) -> list[str]:
    return [row.run_id for row in rows if retirement_mass(row) >= threshold]


def console_snapshot(
    rows: list[ScoreSidecar],
    control_arm_lift: float | None,
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Four tower numbers. Lift is the caller's value, not recomputed here."""

    world = World()
    for row in rows:
        world.sidecars[row.run_id] = row
    view = snapshot_views(world, 0.0 if control_arm_lift is None else control_arm_lift)
    view["control_arm_lift"] = control_arm_lift
    view["label"] = _improvement_label(view, previous)
    return view


def gamma_turn_from_record(record: dict[str, Any] | None) -> tuple[float, str]:
    """Missing or unapproved T2 record: 0.95 and ``constants_version='unset'``."""

    if not record or not str(record.get("approved_by") or "").strip():
        return DEFAULT_GAMMA_TURN, "unset"
    raw = record.get("gamma_turn", DEFAULT_GAMMA_TURN)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_GAMMA_TURN, "unset"
    gamma = float(raw)
    if not (0.0 < gamma <= 1.0):
        return DEFAULT_GAMMA_TURN, "unset"
    version = str(record.get("constants_version") or "unset")
    return gamma, version


def refuse_unapproved_constants(record: dict[str, Any] | None) -> None:
    """An empty ``approved_by`` cannot be installed. This does not mint a token."""

    if not record or not str(record.get("approved_by") or "").strip():
        raise PermissionError("empty approved_by cannot be installed")


def record_turn_credit(
    ledger: HashChainLedger,
    *,
    artifact_id: str,
    turns: list[TurnView],
    prioritized: float,
    policy_record: dict[str, Any] | None,
) -> list[Any]:
    gamma, version = gamma_turn_from_record(policy_record)
    rows = attribute(
        artifact_id,
        turns,
        prioritized,
        gamma_turn=gamma,
        constants_version=version,
    )
    for row in rows:
        ledger.append(
            actor=_ACTOR,
            action="write",
            target=artifact_id,
            evidence={
                "kind": "rsi_step_credit",
                "artifact_id": row.artifact_id,
                "turn_id": row.turn_id,
                "credit": row.credit,
                "gamma_turn": row.gamma_turn,
                "constants_version": row.constants_version,
            },
        )
    return rows


def cases_from_store(store: EpisodicStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in store.list_index():
        rows.append(
            {
                "case_id": row.get("case_id"),
                "task_class": row.get("task_class"),
                "outcome": row.get("outcome"),
            }
        )
    return rows


def stratified_cases(cases: list[dict[str, Any]], *, per_class: int = 1) -> list[dict[str, Any]]:
    """One slice per task class. Does not solve and does not reorder inside a class."""

    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        key = str(case.get("task_class") or "unset")
        buckets.setdefault(key, []).append(case)
    sample: list[dict[str, Any]] = []
    for key in sorted(buckets):
        sample.extend(buckets[key][:per_class])
    return sample


def sample_failure_count(sample: list[dict[str, Any]]) -> int:
    allowed = {"solved", "failed", "abandoned"}
    return sum(1 for case in sample if case.get("outcome") not in allowed)


def golden_expect_validator(golden_root: Path) -> Callable[[SkillVersion], bool]:
    """Read ``expect.json``. Do not run the golden harness."""

    root = Path(golden_root)

    def validator(skill: SkillVersion) -> bool:
        path = root / skill.task_class / skill.skill_id / "expect.json"
        if not path.is_file():
            matches = list(root.glob(f"*/{skill.skill_id}/expect.json"))
            if not matches:
                return False
            path = matches[0]
        doc = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(doc, dict) and doc.get("terminal") == "solved"

    return validator


def discover_golden_skills(golden_root: Path) -> list[SkillVersion]:
    skills: list[SkillVersion] = []
    for expect in sorted(Path(golden_root).glob("*/*/expect.json")):
        task_class = expect.parent.parent.name
        if task_class.startswith("_"):
            continue
        skills.append(
            SkillVersion(
                skill_id=expect.parent.name,
                task_class=task_class,
                lineage=SkillLineage(
                    screened_artifact_id="",
                    screened_payload_sha256="",
                    criteria_hash="",
                ),
            )
        )
    return skills


def production_recalibrate(
    *,
    golden_root: Path,
    cases: list[dict[str, Any]],
    practice_job_s: float,
    duration_s: float,
    index: DurableIndex,
    snapshot_id: str,
) -> RecalibrationManifest:
    """Validator-only pass. A failed manifest does not move the live pointer."""

    index.resume(passed=False)
    live_before = index.live_generation()
    world = World()
    world.practice_job_s = practice_job_s
    skills = discover_golden_skills(golden_root)
    for skill in skills:
        world.skills[skill.skill_id] = skill
    sample = stratified_cases(cases)
    memory = RetrievalIndex(world)
    manifest = run_recalibration(
        world,
        memory,
        snapshot_id,
        [skill.skill_id for skill in skills],
        [golden_expect_validator(golden_root)],
        sample_failures=sample_failure_count(sample),
        sample_size=len(sample),
        duration_s=duration_s,
        must_pass_sample=True,
    )
    if memory.live != 0:
        raise RuntimeError("recalibration moved the library index")
    if not manifest.passed:
        if index.live_generation() != live_before:
            raise RuntimeError("failed manifest moved the live pointer")
        return manifest
    index.prepare(snapshot_id, list(manifest.candidate_skill_ids), manifest.manifest_id)
    index.commit_generation(
        snapshot_id=snapshot_id,
        manifest_id=manifest.manifest_id,
        passed=True,
    )
    return manifest


def write_lineage(
    ledger: HashChainLedger,
    *,
    skill_id: str,
    screened_artifact_id: str,
    payload_sha256: str,
    criteria_hash: str,
    recalibration_manifest_id: str,
) -> str:
    """Ledger row keyed by skill id. Not a field on ``contracts.SkillVersion``."""

    if not skill_id:
        raise ValueError("skill_id is required")
    entry = ledger.append(
        actor=_ACTOR,
        action="write",
        target=skill_id,
        evidence={
            "kind": "rsi_lineage",
            "skill_id": skill_id,
            "screened_artifact_id": screened_artifact_id,
            "payload_sha256": payload_sha256,
            "criteria_hash": criteria_hash,
            "recalibration_manifest_id": recalibration_manifest_id,
        },
    )
    return str(entry.entry_hash)


class DurableIndex:
    """Live generation is the last commit or restore. Prepare does not publish it."""

    def __init__(self, ledger: HashChainLedger) -> None:
        self.ledger = ledger

    def _events(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for entry in self.ledger.entries():
            evidence = entry.evidence or {}
            kind = evidence.get("kind")
            if kind in {
                "rsi_index_prepared",
                "rsi_index_committed",
                "rsi_index_aborted",
                "rsi_index_restored",
            }:
                found.append(evidence)
        return found

    def live_generation(self) -> int:
        live = 0
        for evidence in self._events():
            if evidence["kind"] in {"rsi_index_committed", "rsi_index_restored"}:
                live = int(evidence["generation"])
        return live

    def prepared(self) -> dict[str, Any] | None:
        open_prepare: dict[str, Any] | None = None
        for evidence in self._events():
            if evidence["kind"] == "rsi_index_prepared":
                open_prepare = evidence
            elif evidence["kind"] in {
                "rsi_index_committed",
                "rsi_index_aborted",
                "rsi_index_restored",
            }:
                open_prepare = None
        return open_prepare

    def live_ids(self) -> list[str]:
        generation = self.live_generation()
        found: list[str] = []
        for evidence in self._events():
            if evidence.get("kind") != "rsi_index_prepared":
                continue
            if int(evidence.get("generation", -1)) == generation:
                found = [str(item) for item in evidence.get("skill_ids") or []]
        return found

    def retrieve(self, skill_id: str) -> list[str]:
        if skill_id in self.live_ids():
            return [skill_id]
        return []

    def prepare(self, snapshot_id: str, skill_ids: list[str], manifest_id: str) -> int:
        if self.prepared() is not None:
            raise RuntimeError("prepare already open")
        generation = self.live_generation() + 1
        self._append(
            target=f"index:{generation}",
            evidence={
                "kind": "rsi_index_prepared",
                "snapshot_id": snapshot_id,
                "generation": generation,
                "manifest_id": manifest_id,
                "skill_ids": list(skill_ids),
            },
        )
        if self.live_generation() == generation:
            raise RuntimeError("prepare moved the live pointer")
        return generation

    def commit_generation(self, *, snapshot_id: str, manifest_id: str, passed: bool) -> int:
        prep = self.prepared()
        if prep is None:
            raise RuntimeError("nothing prepared")
        if not passed:
            raise PermissionError("commit requires a passed recalibration manifest")
        if prep.get("manifest_id") != manifest_id or prep.get("snapshot_id") != snapshot_id:
            raise PermissionError("prepared manifest does not match")
        generation = int(prep["generation"])
        self._append(
            target=f"index:{generation}",
            evidence={
                "kind": "rsi_index_committed",
                "snapshot_id": snapshot_id,
                "generation": generation,
                "manifest_id": manifest_id,
            },
        )
        if self.live_generation() != generation:
            raise RuntimeError("commit did not move the pointer")
        return generation

    def abort(self) -> None:
        prep = self.prepared()
        if prep is None:
            return
        generation = int(prep["generation"])
        self._append(
            target=f"index:{generation}",
            evidence={
                "kind": "rsi_index_aborted",
                "snapshot_id": prep.get("snapshot_id"),
                "generation": generation,
                "manifest_id": prep.get("manifest_id"),
            },
        )
        if self.live_generation() == generation:
            raise RuntimeError("abort moved the live pointer")

    def restore(self, generation: int) -> None:
        self._append(
            target=f"index:{generation}",
            evidence={"kind": "rsi_index_restored", "generation": generation},
        )

    def resume(
        self,
        *,
        passed: bool | None,
        snapshot_id: str | None = None,
        manifest_id: str | None = None,
    ) -> str:
        """A trailing prepare is committed only when the manifest passed. Else abort."""

        prep = self.prepared()
        if prep is None:
            return "idle"
        if (
            passed is True
            and snapshot_id == prep.get("snapshot_id")
            and manifest_id == prep.get("manifest_id")
        ):
            self.commit_generation(
                snapshot_id=str(snapshot_id),
                manifest_id=str(manifest_id),
                passed=True,
            )
            return "committed"
        self.abort()
        return "aborted"

    def _append(self, *, target: str, evidence: dict[str, Any]) -> None:
        self.ledger.append(actor=_ACTOR, action="write", target=target, evidence=evidence)


def append_stage(
    ledger: HashChainLedger,
    *,
    snapshot_id: str,
    stage: str,
    index_generation: int,
    active_skill_ids: list[str],
) -> None:
    ledger.append(
        actor=_ACTOR,
        action="write",
        target=snapshot_id,
        evidence={
            "kind": "rsi_stage",
            "snapshot_id": snapshot_id,
            "stage": stage,
            "index_generation": index_generation,
            "active_skill_ids": list(active_skill_ids),
        },
    )


def rollback_dry_run(
    ledger: HashChainLedger,
    index: DurableIndex,
    *,
    baseline_snapshot_id: str,
    post_change_skill_id: str,
) -> dict[str, Any]:
    """Force the recorded flags off, restore the pointer, keep every receipt."""

    promote = None
    for entry in ledger.entries():
        evidence = entry.evidence or {}
        if (
            evidence.get("kind") == "rsi_stage"
            and evidence.get("stage") == "promote"
            and evidence.get("snapshot_id") == baseline_snapshot_id
        ):
            promote = evidence
    if promote is None:
        raise KeyError("no baseline promote entry")
    before = [entry.entry_hash for entry in ledger.entries()]
    generation = int(promote["index_generation"])
    index.restore(generation)
    append_stage(
        ledger,
        snapshot_id=baseline_snapshot_id,
        stage="rollback",
        index_generation=generation,
        active_skill_ids=list(promote.get("active_skill_ids") or []),
    )
    ledger.append(
        actor=_ACTOR,
        action="write",
        target=baseline_snapshot_id,
        evidence={
            "kind": "rsi_incident",
            "incident": "rollback",
            "generation": generation,
            "hygiene_enabled": False,
            "calibrated_scoring": False,
        },
    )
    after = ledger.entries()
    if [entry.entry_hash for entry in after[: len(before)]] != before:
        raise RuntimeError("rollback rewrote a receipt")
    if index.retrieve(post_change_skill_id):
        raise RuntimeError("post-change skill still retrieves")
    if index.live_generation() != generation:
        raise RuntimeError("pointer was not restored")
    return {
        "generation": generation,
        "receipts": len(after),
        "hygiene_enabled": False,
        "calibrated_scoring": False,
    }


def _task_class(doc: dict[str, Any]) -> str:
    parts = Path(str(doc.get("golden_path") or "")).parts
    if "golden" in parts:
        index = parts.index("golden")
        if index + 1 < len(parts):
            return str(parts[index + 1])
    return "unset"


def _improvement_label(view: dict[str, Any], previous: dict[str, Any] | None) -> str | None:
    if previous is None:
        return None
    process_moved = view["process_score"] != previous.get("process_score")
    lift_same = view["control_arm_lift"] == previous.get("control_arm_lift")
    if process_moved and lift_same:
        return "not an improvement"
    return None
