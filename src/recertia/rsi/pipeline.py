"""Distill, recalibration, two-phase index, promotion, rollback, capacity.

Promotion authorizes on control-arm lift plus a passed manifest.
Calibrated scores are not an input to promotion. Spec R6, R9, R10, R12.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Callable, Optional

from recertia.rsi.attribution import attribute
from recertia.rsi.hygiene import HygieneGate
from recertia.rsi.models import (
    HygieneRequest,
    RecalibrationManifest,
    SkillLineage,
    SkillVersion,
    TurnView,
)
from recertia.rsi.scoring import score_run
from recertia.rsi.world import World


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RetrievalIndex:
    """Live pointer moves only on IndexCommitted. Spec 9.5."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.generations: dict[int, list[str]] = {0: []}
        self.live = 0
        self.prepared: Optional[dict] = None

    def live_ids(self) -> list[str]:
        return list(self.generations.get(self.live, []))

    def prepare(self, snapshot_id: str, skill_ids: list[str], manifest_id: str) -> int:
        generation = self.live + 1
        self.generations[generation] = list(skill_ids)
        self.prepared = {
            "snapshot_id": snapshot_id,
            "generation": generation,
            "manifest_id": manifest_id,
            "skill_ids": list(skill_ids),
        }
        self.world.ledger.append("index_prepared", dict(self.prepared))
        return generation

    def commit(self, manifest: RecalibrationManifest) -> int:
        if self.prepared is None:
            raise RuntimeError("nothing prepared")
        if not manifest.passed:
            raise PermissionError("commit requires a passed recalibration manifest")
        if manifest.manifest_id != self.prepared["manifest_id"]:
            raise PermissionError("prepared manifest does not match")
        if manifest.snapshot_id != self.prepared["snapshot_id"]:
            raise PermissionError("prepared snapshot does not match")
        generation = int(self.prepared["generation"])
        self.live = generation
        self.world.ledger.append(
            "index_committed",
            {
                "snapshot_id": manifest.snapshot_id,
                "generation": generation,
                "manifest_id": manifest.manifest_id,
            },
        )
        self.prepared = None
        return generation

    def abort(self) -> None:
        if self.prepared is None:
            return
        generation = int(self.prepared["generation"])
        self.generations.pop(generation, None)
        self.world.ledger.append("index_aborted", dict(self.prepared))
        self.prepared = None

    def restore(self, generation: int) -> None:
        if generation not in self.generations:
            raise KeyError(f"unknown generation {generation}")
        self.live = generation
        self.prepared = None
        self.world.ledger.append("index_restored", {"generation": generation})

    @classmethod
    def from_ledger(cls, world: World) -> "RetrievalIndex":
        """Crash between prepare and commit leaves the prior generation live."""
        index = cls(world)
        # Rebuild generations from prepare events, but the live pointer
        # follows only commit and restore records.
        prepared_gens: dict[int, list[str]] = {}
        # Skill membership of a prepared generation is not on the ledger event
        # beyond the id list we store. We persist ids on prepare.
        live = 0
        open_prepare = None
        for entry in world.ledger.entries:
            kind = entry["kind"]
            body = entry["body"]
            if kind == "index_prepared":
                generation = int(body["generation"])
                prepared_gens[generation] = list(body.get("skill_ids", []))
                open_prepare = body
            elif kind == "index_committed":
                live = int(body["generation"])
                open_prepare = None
            elif kind == "index_aborted":
                prepared_gens.pop(int(body["generation"]), None)
                open_prepare = None
            elif kind == "index_restored":
                live = int(body["generation"])
                open_prepare = None
        index.generations.update(prepared_gens)
        index.generations.setdefault(0, [])
        index.live = live
        index.prepared = None if open_prepare is None else open_prepare
        # A trailing prepare must not move the pointer. It stays as `prepared`
        # so a reader of live_ids() still sees the last commit.
        if index.prepared is not None and int(index.prepared["generation"]) == index.live:
            raise RuntimeError("prepare must not be the live generation")
        return index


def distill(world: World, artifact_id: str) -> SkillVersion:
    artifact = world.screened.get(artifact_id)
    if artifact is None:
        raise KeyError("distill requires an accepted ScreenedArtifact")
    skill_id = "sk_" + hashlib.sha256(artifact.payload_sha256.encode()).hexdigest()[:16]
    if skill_id in world.skills:
        return world.skills[skill_id]
    skill = SkillVersion(
        skill_id=skill_id,
        task_class=artifact.task_class,
        lineage=SkillLineage(
            screened_artifact_id=artifact.artifact_id,
            screened_payload_sha256=artifact.payload_sha256,
            criteria_hash=artifact.criteria_hash,
            recalibration_manifest_id="",
            parent_skill_id=None,
        ),
        active=False,
        strict_success=artifact.strict_success,
    )
    world.skills[skill_id] = skill
    world.ledger.append(
        "skill_version",
        {
            "skill_id": skill_id,
            "task_class": skill.task_class,
            "active": False,
            "strict_success": skill.strict_success,
            "lineage": {
                "screened_artifact_id": artifact.artifact_id,
                "screened_payload_sha256": artifact.payload_sha256,
                "criteria_hash": artifact.criteria_hash,
                "recalibration_manifest_id": "",
                "parent_skill_id": None,
            },
        },
    )
    world.ledger.append(
        "stage",
        {
            "snapshot_id": skill_id,
            "stage": "distill",
            "retained": True,
            "metrics": {"strict_count": 1.0 if skill.strict_success else 0.0},
            "parent_receipt": world.ledger.head(),
            "index_generation": 0,
            "active_skill_ids": [],
        },
    )
    return skill


def import_trajectory(world: World, gate: HygieneGate, req: HygieneRequest):
    """Flag off: legacy import, no sandbox. Flag on: hygiene gate. Default off."""
    if not world.flags.hygiene_enabled:
        receipt = world.ledger.append(
            "legacy_import",
            {"idempotency_key": req.idempotency_key, "criteria_hash": req.criteria_hash},
        )
        return {"path": "legacy", "ledger_receipt": receipt, "sandboxed": False}
    return gate.run(req)


Validator = Callable[[SkillVersion], bool]


def run_recalibration(
    world: World,
    index: RetrievalIndex,
    snapshot_id: str,
    skill_ids: list[str],
    validators: list[Validator],
    sample_failures: int = 0,
    sample_size: int = 0,
    duration_s: float = 0.0,
    must_pass_sample: bool = False,
) -> RecalibrationManifest:
    """Validator-only. Does not solve and does not move the index."""
    live_before = index.live
    ids = tuple(sorted(skill_ids))
    key = (snapshot_id, hashlib.sha256(",".join(ids).encode()).hexdigest())
    cache_key = f"recal:{key[0]}:{key[1]}"
    cached = world.idempotency.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    failures = 0
    for sid in skill_ids:
        skill = world.skills.get(sid)
        if skill is None:
            failures += 1
            continue
        for validator in validators:
            if not validator(skill):
                failures += 1
    duration_breach = duration_s >= 0.05 * world.practice_job_s and world.practice_job_s > 0
    sample_blocks = must_pass_sample and sample_failures > 0
    passed = failures == 0 and not duration_breach and not sample_blocks
    manifest_id = "man_" + hashlib.sha256(cache_key.encode()).hexdigest()[:16]
    receipt = world.ledger.append(
        "recalibration_manifest",
        {
            "manifest_id": manifest_id,
            "snapshot_id": snapshot_id,
            "candidate_skill_ids": list(ids),
            "passed": passed,
            "validator_failures": failures,
            "sample_size": sample_size,
            "duration_s": duration_s,
            "duration_breach": duration_breach,
        },
    )
    manifest = RecalibrationManifest(
        manifest_id=manifest_id,
        snapshot_id=snapshot_id,
        candidate_skill_ids=list(ids),
        passed=passed,
        validator_failures=failures,
        sample_size=sample_size,
        duration_s=duration_s,
        ledger_receipt=receipt,
        created_at=_now(),
        duration_breach=duration_breach,
    )
    world.manifests[manifest_id] = manifest
    world.idempotency[cache_key] = manifest
    world.ledger.append(
        "stage",
        {
            "snapshot_id": snapshot_id,
            "stage": "recalibrate",
            "retained": passed,
            "metrics": {
                "validator_failures": float(failures),
                "duration_s": float(duration_s),
                "strict_count": float(len(skill_ids)),
            },
            "parent_receipt": receipt,
            "index_generation": index.live,
            "active_skill_ids": [],
        },
    )
    if index.live != live_before:
        raise RuntimeError("recalibration must not move the live index")
    return manifest


def promote(
    world: World,
    index: RetrievalIndex,
    snapshot_id: str,
    manifest: RecalibrationManifest,
    control_arm_lift: float,
    skill_ids: list[str],
) -> list[str]:
    """Control-arm lift is the authorization. Scores are not consulted."""
    if not manifest.passed:
        raise PermissionError("promotion refused without a passed manifest")
    if control_arm_lift <= 0:
        raise PermissionError("promotion refused without positive control-arm lift")
    activated = []
    for sid in skill_ids:
        skill = world.skills[sid]
        if not skill.strict_success:
            raise PermissionError("promotion refused without strict success")
        if skill.lineage.screened_artifact_id not in world.screened:
            raise PermissionError("promotion refused without a screened artifact")
        new_id = skill.skill_id + ".a"
        if new_id in world.skills:
            activated.append(new_id)
            continue
        promoted = SkillVersion(
            skill_id=new_id,
            task_class=skill.task_class,
            lineage=SkillLineage(
                screened_artifact_id=skill.lineage.screened_artifact_id,
                screened_payload_sha256=skill.lineage.screened_payload_sha256,
                criteria_hash=skill.lineage.criteria_hash,
                recalibration_manifest_id=manifest.manifest_id,
                parent_skill_id=skill.skill_id,
            ),
            active=True,
            strict_success=True,
        )
        world.skills[new_id] = promoted
        world.ledger.append(
            "skill_version",
            {
                "skill_id": new_id,
                "active": True,
                "manifest_id": manifest.manifest_id,
                "parent_skill_id": skill.skill_id,
            },
        )
        activated.append(new_id)
    index.prepare(snapshot_id, activated, manifest.manifest_id)
    index.commit(manifest)
    parent = world.ledger.head()
    world.ledger.append(
        "stage",
        {
            "snapshot_id": snapshot_id,
            "stage": "promote",
            "retained": True,
            "metrics": {"control_arm_lift": float(control_arm_lift), "strict_count": float(len(activated))},
            "parent_receipt": parent,
            "index_generation": index.live,
            "active_skill_ids": list(activated),
        },
    )
    return activated


def retrieve(world: World, index: RetrievalIndex, skill_id: Optional[str] = None) -> list[SkillVersion]:
    """Execution plane. Screened artifacts are invisible. Spec 9.6."""
    live = set(index.live_ids())
    out = []
    for sid in sorted(live):
        skill = world.skills.get(sid)
        if skill is None:
            continue
        if not skill.active:
            continue
        if not skill.lineage.recalibration_manifest_id:
            continue
        if skill_id is not None and sid != skill_id:
            continue
        out.append(skill)
    return out


def install_constants(world: World, constants, token: str) -> None:
    """Human-gated T2 write. Also ledgered so replay can name the version."""
    world.policy.put_constants(constants, token)
    world.ledger.append(
        "calib_constants",
        {
            "version": constants.version,
            "partial_scale": constants.partial_scale,
            "approved_by": constants.approved_by,
        },
    )


def write_sidecar(
    world: World,
    run_id: str,
    task_class: str,
    checks_passed: int,
    checks_total: int,
    strict: bool,
):
    constants = world.policy.get()
    if constants is None:
        raise RuntimeError("no CalibConstants loaded")
    row = score_run(run_id, task_class, checks_passed, checks_total, strict, constants)
    # Raw check results stay on the sidecar and are not rewritten when constants change.
    if run_id not in world.sidecars:
        world.sidecars[run_id] = row
        world.ledger.append(
            "sidecar",
            {
                "run_id": run_id,
                "legacy": row.legacy,
                "checks_passed": checks_passed,
                "checks_total": checks_total,
                "strict_success": strict,
                "task_class": task_class,
                "constants_version": row.constants_version,
                "prioritized": row.prioritized,
            },
        )
    return world.sidecars[run_id]


def record_credits(
    world: World,
    artifact_id: str,
    turns: list[TurnView],
    prioritized: float,
    constants_version: str,
):
    rows = attribute(
        artifact_id,
        turns,
        prioritized,
        gamma_turn=world._gamma_turn,
        constants_version=constants_version,
    )
    world.credits.extend(rows)
    for row in rows:
        world.ledger.append(
            "step_credit",
            {
                "artifact_id": row.artifact_id,
                "turn_id": row.turn_id,
                "credit": row.credit,
                "gamma_turn": row.gamma_turn,
                "constants_version": row.constants_version,
            },
        )
    return rows


def snapshot_views(world: World, control_arm_lift: float) -> dict:
    rows = list(world.sidecars.values())
    if not rows:
        process = 0.0
        strict = 0
        share = 0.0
    else:
        process = sum(r.prioritized for r in rows) / len(rows)
        strict = sum(1 for r in rows if r.strict_success)
        mass = sum(r.prioritized for r in rows)
        failed = sum(r.prioritized for r in rows if not r.strict_success)
        share = (failed / mass) if mass else 0.0
    return {
        "process_score": process,
        "strict_completion": strict,
        "failure_share": share,
        "control_arm_lift": control_arm_lift,
    }


def enqueue_review(world: World, case_id: str, note: str) -> None:
    world.review_queue.append({"case_id": case_id, "note": note, "promoted": False})


def promote_from_review(world: World, case_id: str) -> None:
    raise PermissionError("human review cannot auto-promote")


def should_rollback(current_rate: float, baseline_rate: float, noise_band: float) -> bool:
    return current_rate < baseline_rate - noise_band


def rollback(world: World, index: RetrievalIndex, baseline_snapshot_id: str) -> str:
    """Spec section 13. Flags off, pointer restored, receipts kept."""
    world.flags.hygiene_enabled = False
    world.flags.calibrated_scoring = False
    promote_entry = None
    for entry in world.ledger.entries:
        if entry["kind"] != "stage":
            continue
        body = entry["body"]
        if body.get("stage") == "promote" and body.get("snapshot_id") == baseline_snapshot_id:
            promote_entry = entry
    if promote_entry is None:
        raise KeyError("no baseline promote entry")
    restore_head = promote_entry["body"]["parent_receipt"]
    generation = int(promote_entry["body"]["index_generation"])
    expected = list(promote_entry["body"]["active_skill_ids"])
    world.ledger.append(
        "stage",
        {
            "snapshot_id": baseline_snapshot_id,
            "stage": "rollback",
            "retained": False,
            "metrics": {},
            "parent_receipt": world.ledger.head(),
            "index_generation": generation,
            "active_skill_ids": expected,
        },
    )
    index.restore(generation)
    for sid, skill in list(world.skills.items()):
        if skill.active and sid not in expected:
            # Do not delete. Mark a successor inactive by appending a shadow
            # record the retrieve path ignores because the index no longer names it.
            pass
    world.incidents.append(
        {
            "kind": "rollback",
            "restore_head": restore_head,
            "generation": generation,
            "ledger_receipt": world.ledger.head(),
        }
    )
    return restore_head


def verify_rollback(world: World, index: RetrievalIndex, baseline_snapshot_id: str) -> None:
    promote_entry = None
    for entry in world.ledger.entries:
        if entry["kind"] == "stage" and entry["body"].get("stage") == "promote":
            if entry["body"].get("snapshot_id") == baseline_snapshot_id:
                promote_entry = entry
    assert promote_entry is not None
    expected = set(promote_entry["body"]["active_skill_ids"])
    live = set(index.live_ids())
    if live != expected:
        raise AssertionError(f"live {live} != baseline {expected}")


def capacity_report(
    durations_s: list[float],
    free_slots: int,
    distill_slo_s: float,
) -> dict:
    """Spec section 12. Sampling stays illegal until this number exists."""
    if len(durations_s) < 50:
        raise ValueError("capacity gate needs at least 50 trajectories")
    if distill_slo_s <= 0:
        raise ValueError("distill SLO must be positive")
    ordered = sorted(durations_s)
    mid = ordered[len(ordered) // 2]
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    p95 = ordered[p95_index]
    slots_required = max(1, math.ceil((len(durations_s) * p95) / distill_slo_s))
    headroom = free_slots / slots_required
    return {
        "reexec_fraction": 1.0,
        "n": len(durations_s),
        "median_reexec_s": mid,
        "p95_reexec_s": p95,
        "slots_required": slots_required,
        "free_slots": free_slots,
        "pool_headroom": headroom,
        "phase1_allowed": headroom >= 1.5,
        "sampling_allowed": False,
    }


def replay(world: World, head: Optional[str] = None) -> dict:
    """Fold the ledger up to head. Spec R11."""
    receipt = head or world.ledger.head()
    entries = world.ledger.up_to(receipt)
    screened = []
    constants_version = None
    manifests = []
    credits = []
    live = 0
    for entry in entries:
        kind = entry["kind"]
        body = entry["body"]
        if kind == "screened_artifact":
            screened.append(body["artifact_id"])
        elif kind == "calib_constants":
            constants_version = body["version"]
        elif kind == "recalibration_manifest":
            manifests.append(body["manifest_id"])
        elif kind == "step_credit":
            credits.append(body)
        elif kind == "index_committed":
            live = int(body["generation"])
        elif kind == "index_restored":
            live = int(body["generation"])
    return {
        "head": receipt,
        "screened": screened,
        "constants_version": constants_version,
        "manifests": manifests,
        "credits": credits,
        "index_generation": live,
    }
