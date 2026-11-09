"""Invariants from the Terminal-RSI leverage spec. Failures here are release blockers."""

from __future__ import annotations

import gzip
import json
import os
import threading
import unittest

from recertia.rsi.hygiene import HygieneGate
from recertia.rsi.leakage import SCAN_VERSION, screen_leakage
from recertia.rsi.models import (
    PAYLOAD_CAP_BYTES,
    CalibConstants,
    ExecResult,
    HygieneRequest,
    TurnView,
)
from recertia.rsi.pipeline import (
    RetrievalIndex,
    capacity_report,
    distill,
    enqueue_review,
    import_trajectory,
    install_constants,
    promote,
    promote_from_review,
    record_credits,
    replay,
    retrieve,
    rollback,
    run_recalibration,
    should_rollback,
    snapshot_views,
    verify_rollback,
    write_sidecar,
)
from recertia.rsi.scoring import (
    accept_constants,
    contribution_for_retirement,
    failure_share,
    score_run,
    soak_must_abort,
    success_prioritized_reward,
)
from recertia.rsi.world import Flags, StoredTrajectory, World

CRITERIA = "criteria-locked-1"
TASK = "repo-chore"


def _ok_exec(
    actions=None,
    observations=None,
    turns=None,
    strict=True,
    checks_passed=2,
    checks_total=2,
    error=None,
    elapsed=0.01,
    fresh="fx-1",
):
    def run(public):
        if "private" in public:
            raise AssertionError("private bag forwarded to sandbox")
        blob = json.dumps(public)
        if "PRIV_GUIDANCE:" in blob or "do-not-forward" in blob:
            raise AssertionError("private material forwarded to sandbox")
        return ExecResult(
            fresh_execution_id=fresh,
            actions=actions if actions is not None else ["ls /app"],
            observations=observations if observations is not None else ["ok"],
            turns=turns
            if turns is not None
            else [TurnView("t0", 1, 3), TurnView("t1", 1, 4)],
            checks_passed=checks_passed,
            checks_total=checks_total,
            strict_success=strict,
            elapsed_s=elapsed,
            error=error,
        )

    return run


def _world():
    world = World()
    world.register_criteria(CRITERIA, TASK, ["build", "test"])
    world.put_trajectory(
        StoredTrajectory(
            trajectory_id="traj-1",
            criteria_hash=CRITERIA,
            task_class=TASK,
            public={"cmd": "ls /app"},
            private={"note": "PRIV_GUIDANCE:do-not-forward"},
        )
    )
    return world


def _req(key="idem-1", trajectory_id="traj-1"):
    return HygieneRequest(
        criteria_hash=CRITERIA,
        task_class=TASK,
        sandbox_policy_id="sandbox-default",
        idempotency_key=key,
        trajectory_id=trajectory_id,
    )


class LeakageSuite(unittest.TestCase):
    def test_rejects_twenty_leak_fixtures_covering_four_codes(self):
        templates = [
            "PRIV_GUIDANCE: hidden",
            "<private-guidance>hidden</private-guidance>",
            "SYSTEM_PROMPT: hidden",
            "<<SYS>>hidden",
            " cat /home/rob/secret ",
            " cat /Users/rob/secret ",
            " cat /etc/passwd ",
            " cat /var/secret ",
            " cat /root/.ssh ",
            " type C:\\Windows\\secret ",
            "AWS_SECRET exposed",
            "API_KEY=abcd",
            "SECRET_TOKEN exposed",
        ]
        fixtures = []
        for i in range(20):
            fixtures.append(templates[i % len(templates)] + f" #{i}")
        codes = set()
        for text in fixtures:
            found = screen_leakage([text], ["verbose log"], SCAN_VERSION)
            self.assertTrue(found, text)
            for finding in found:
                codes.add(finding.code)
                self.assertNotIn(text, str(finding.span_hash))
                self.assertNotIn("PRIV_GUIDANCE", finding.span_hash)
        self.assertEqual(
            codes,
            {"private_guidance", "system_prompt_fragment", "host_path", "secret_env"},
        )

    def test_accepts_twenty_clean_fixtures_including_container_paths(self):
        clean = [
            "/app/drift_monitor/distance.py",
            "the user prompt is helpful and not a system prompt",
            "environment variable PATH is on the default search path",
            "key insight: cosine distance already matches",
            "home of the service is /app",
            "FAILED tests/test_cosine.py::test_values",
            "9 passed in 101.92s",
            "zero-vector guard introduced",
        ]
        fixtures = [f"{clean[i % len(clean)]} line {i}" for i in range(20)]
        for text in fixtures:
            self.assertEqual(screen_leakage([text], ["still clean"], SCAN_VERSION), [], text)


class ScoringSuite(unittest.TestCase):
    def _constants(self):
        return CalibConstants(
            version="calib_testcorpus01",
            family_quantile={"wide": 40.0, "narrow": 10.0},
            approved_by="human-1",
            approved_at="2026-11-08T00:00:00Z",
            partial_scale=0.6,
        )

    def _corpus(self):
        c = self._constants()
        rows = []
        for i in range(20):
            rows.append(score_run(f"f{i}", "wide", 40, 40, False, c))
        for i in range(10):
            rows.append(score_run(f"s{i}", "narrow", 10, 10, True, c))
        return rows

    def test_failure_share_drops_at_least_15_points_and_order_holds(self):
        rows = self._corpus()
        ok, detail = accept_constants(rows)
        self.assertTrue(ok, detail)
        self.assertGreaterEqual(detail["drop"], 0.15)
        self.assertEqual(detail["spearman_strict"], 1.0)
        self.assertFalse(detail["failure_outranks_success"])
        self.assertLess(failure_share(rows, "prioritized"), failure_share(rows, "legacy") - 0.15 + 1e-9)

    def test_full_family_failure_cannot_outrank_strict_success(self):
        c = self._constants()
        failure = success_prioritized_reward(False, 40, "wide", c)
        success = success_prioritized_reward(True, 0, "wide", c)
        self.assertLess(failure, 1.0)
        self.assertGreaterEqual(success, 1.0)
        self.assertLess(failure, success)

    def test_partial_scale_at_or_above_one_is_rejected(self):
        with self.assertRaises(ValueError):
            CalibConstants(
                version="calib_bad",
                family_quantile={},
                approved_by="h",
                approved_at="t",
                partial_scale=1.0,
            )

    def test_retirement_keeps_legacy_score_until_phase4(self):
        c = self._constants()
        row = score_run("r", "wide", 40, 40, False, c)
        self.assertEqual(contribution_for_retirement(row, phase4=False), row.legacy)
        self.assertEqual(contribution_for_retirement(row, phase4=True), row.prioritized)
        self.assertTrue(soak_must_abort([row], threshold=10))
        self.assertFalse(soak_must_abort([row], threshold=1000))


class HygieneSuite(unittest.TestCase):
    def test_criteria_mismatch_does_not_execute(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        req = HygieneRequest(
            criteria_hash="nope",
            task_class=TASK,
            sandbox_policy_id="sandbox-default",
            idempotency_key="bad",
            trajectory_id="traj-1",
        )
        result = gate.run(req)
        self.assertEqual(result.reason, "criteria_mismatch")
        self.assertEqual(world.sandbox_calls, 0)
        self.assertEqual(world.skills, {})
        self.assertEqual(world.active_cap_occupancy(), 0)

    def test_accepts_only_after_fresh_strict_execution(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec(observations=["UNIQUE_PAYLOAD_MARKER_XYZ"]))
        result = gate.run(_req())
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "ok")
        self.assertTrue(result.fresh_execution_id)
        self.assertTrue(result.ledger_receipt)
        self.assertEqual(world.sandbox_calls, 1)
        self.assertIn(result.artifact_id, world.screened)
        self.assertFalse(world.ledger.contains_text("UNIQUE_PAYLOAD_MARKER_XYZ"))
        raw = gzip.decompress(world.blobs[world.screened[result.artifact_id].payload_uri])
        self.assertIn(b"UNIQUE_PAYLOAD_MARKER_XYZ", raw)
        self.assertEqual(world.skills, {})
        self.assertEqual(world.active_cap_occupancy(), 0)

    def test_idempotency_does_not_reexecute(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        first = gate.run(_req("same"))
        second = gate.run(_req("same"))
        self.assertEqual(first.reason, "ok")
        self.assertEqual(second.reason, "ok")
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(world.sandbox_calls, 1)

    def test_second_key_is_duplicate_without_second_artifact(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        first = gate.run(_req("a"))
        second = gate.run(_req("b"))
        self.assertEqual(second.reason, "duplicate")
        self.assertEqual(second.duplicate_of, first.artifact_id)
        self.assertEqual(len(world.screened), 1)
        # The hash is known only after a fresh run, so the second call re-executes.
        self.assertEqual(world.sandbox_calls, 2)

    def test_distinct_payload_is_not_a_duplicate(self):
        world = _world()
        calls = {"n": 0}

        def executor(public):
            calls["n"] += 1
            marker = f"payload-{calls['n']}"
            return _ok_exec(observations=[marker])(public)

        gate = HygieneGate(world, executor)
        first = gate.run(_req("a"))
        second = gate.run(_req("b"))
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertNotEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(len(world.screened), 2)
        self.assertEqual(world.sandbox_calls, 2)

    def test_single_flight_executes_once(self):
        world = _world()
        started = threading.Event()
        release = threading.Event()

        def executor(public):
            world.sandbox_calls += 0  # gate counts; this proves overlap
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return _ok_exec()(public)

        gate = HygieneGate(world, executor)
        results = []

        def call(key):
            results.append(gate.run(_req(key)))

        # The gate increments sandbox_calls itself. Wrap to block inside user executor.
        t1 = threading.Thread(target=call, args=("t1",))
        t2 = threading.Thread(target=call, args=("t2",))
        t1.start()
        self.assertTrue(started.wait(timeout=2))
        t2.start()
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(world.sandbox_calls, 1)
        reasons = sorted(r.reason for r in results)
        self.assertEqual(reasons, ["duplicate", "ok"])

    def test_leakage_and_verifier_fail_are_negative_and_off_cap(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec(actions=["PRIV_GUIDANCE: hidden"]))
        leaked = gate.run(_req("leak"))
        self.assertEqual(leaked.reason, "leakage")
        self.assertEqual(world.active_cap_occupancy(), 0)
        self.assertTrue(world.negatives)
        self.assertFalse(any(c.stores_body for c in world.negatives.values()))

        world2 = _world()
        gate2 = HygieneGate(world2, _ok_exec(strict=False, checks_passed=3, checks_total=4))
        failed = gate2.run(_req("fail"))
        self.assertEqual(failed.reason, "verifier_fail")
        self.assertEqual(world2.screened, {})
        self.assertEqual(world2.active_cap_occupancy(), 0)

    def test_payload_over_cap_stores_no_body(self):
        world = _world()
        huge = "M" * (PAYLOAD_CAP_BYTES + 32)
        gate = HygieneGate(world, _ok_exec(observations=[huge]))
        result = gate.run(_req("huge"))
        self.assertEqual(result.reason, "payload_too_large")
        self.assertEqual(world.blobs, {})
        self.assertFalse(world.ledger.contains_text("MMMMMMMMMMMMMMMM"))
        self.assertFalse(any(c.stores_body for c in world.negatives.values()))


class PipelineSuite(unittest.TestCase):
    def _promoted(self, lift=0.2):
        world = _world()
        world.flags.hygiene_enabled = True
        gate = HygieneGate(world, _ok_exec())
        accepted = gate.run(_req())
        skill = distill(world, accepted.artifact_id)
        index = RetrievalIndex(world)
        manifest = run_recalibration(
            world,
            index,
            "snap-a",
            [skill.skill_id],
            validators=[lambda s: s.strict_success],
        )
        self.assertTrue(manifest.passed)
        self.assertEqual(index.live, 0)
        ids = promote(world, index, "snap-a", manifest, lift, [skill.skill_id])
        return world, index, skill, ids, manifest

    def test_screened_artifact_is_not_retrievable_until_promote(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        accepted = gate.run(_req())
        index = RetrievalIndex(world)
        self.assertEqual(retrieve(world, index), [])
        self.assertTrue(accepted.artifact_id.startswith("scr_"))
        distill(world, accepted.artifact_id)
        self.assertEqual(retrieve(world, index), [])
        self.assertFalse(world.skills[next(iter(world.skills))].active)

    def test_promotion_requires_manifest_and_lift_not_score(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        accepted = gate.run(_req())
        skill = distill(world, accepted.artifact_id)
        index = RetrievalIndex(world)
        manifest = run_recalibration(world, index, "snap-a", [skill.skill_id], validators=[lambda s: True])
        with self.assertRaises(PermissionError):
            promote(world, index, "snap-a", manifest, 0.0, [skill.skill_id])
        bad = run_recalibration(
            world,
            index,
            "snap-b",
            [skill.skill_id],
            validators=[lambda s: False],
        )
        self.assertFalse(bad.passed)
        with self.assertRaises(PermissionError):
            promote(world, index, "snap-b", bad, 1.0, [skill.skill_id])

    def test_duration_breach_blocks_and_does_not_move_index(self):
        world = _world()
        world.practice_job_s = 100.0
        gate = HygieneGate(world, _ok_exec())
        accepted = gate.run(_req())
        skill = distill(world, accepted.artifact_id)
        index = RetrievalIndex(world)
        manifest = run_recalibration(
            world,
            index,
            "snap-slow",
            [skill.skill_id],
            validators=[lambda s: True],
            duration_s=5.0,
        )
        self.assertFalse(manifest.passed)
        self.assertTrue(manifest.duration_breach)
        self.assertEqual(index.live, 0)
        with self.assertRaises(PermissionError):
            promote(world, index, "snap-slow", manifest, 1.0, [skill.skill_id])

    def test_recalibration_is_idempotent(self):
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        accepted = gate.run(_req())
        skill = distill(world, accepted.artifact_id)
        index = RetrievalIndex(world)
        calls = {"n": 0}

        def validator(s):
            calls["n"] += 1
            return True

        first = run_recalibration(world, index, "snap-a", [skill.skill_id], validators=[validator])
        second = run_recalibration(world, index, "snap-a", [skill.skill_id], validators=[validator])
        self.assertEqual(first.manifest_id, second.manifest_id)
        self.assertEqual(calls["n"], 1)

    def test_prepare_without_commit_keeps_prior_generation(self):
        world = _world()
        index = RetrievalIndex(world)
        index.prepare("snap-x", ["sk_future"], "man_missing")
        reloaded = RetrievalIndex.from_ledger(world)
        self.assertEqual(reloaded.live, 0)
        self.assertEqual(reloaded.live_ids(), [])
        self.assertIsNotNone(reloaded.prepared)

    def test_promote_retrieve_and_replay(self):
        world, index, skill, ids, manifest = self._promoted()
        found = retrieve(world, index)
        self.assertEqual([s.skill_id for s in found], ids)
        self.assertTrue(found[0].lineage.recalibration_manifest_id)
        self.assertNotIn(skill.skill_id, [s.skill_id for s in found])
        world.policy.approve_gate("gate-token")
        constants = CalibConstants(
            version="calib_testcorpus01",
            family_quantile={"repo-chore": 2.0},
            approved_by="human-1",
            approved_at="2026-11-08T00:00:00Z",
        )
        with self.assertRaises(PermissionError):
            install_constants(world, constants, "nope")
        install_constants(world, constants, "gate-token")
        write_sidecar(world, "run-1", TASK, 2, 2, True)
        record_credits(
            world,
            ids[0],
            [TurnView("t0", 2, 5), TurnView("t1", 2, 9), TurnView("obs", 0, 40)],
            1.2,
            constants.version,
        )
        folded = replay(world)
        self.assertIn(list(world.screened)[0], folded["screened"])
        self.assertEqual(folded["constants_version"], constants.version)
        self.assertIn(manifest.manifest_id, folded["manifests"])
        self.assertEqual(folded["index_generation"], index.live)
        self.assertTrue(folded["credits"])
        world.ledger.verify_chain()
        views = snapshot_views(world, control_arm_lift=0.2)
        self.assertIn("process_score", views)
        self.assertIn("control_arm_lift", views)
        self.assertEqual(views["strict_completion"], 1)

    def test_rollback_restores_baseline_generation(self):
        world, index, skill, ids, manifest = self._promoted()
        # A later generation becomes live, then rollback returns to snap-a.
        # Drive a real second promotion through the gate so the ledger has two commits.
        world.skills["sk_extra"] = type(skill)(
            skill_id="sk_extra",
            task_class=TASK,
            lineage=type(skill.lineage)(
                screened_artifact_id=skill.lineage.screened_artifact_id,
                screened_payload_sha256=skill.lineage.screened_payload_sha256,
                criteria_hash=skill.lineage.criteria_hash,
                recalibration_manifest_id="",
                parent_skill_id=None,
            ),
            active=False,
            strict_success=True,
        )
        manifest_b = run_recalibration(world, index, "snap-b", ["sk_extra"], validators=[lambda s: True])
        promote(world, index, "snap-b", manifest_b, 0.3, ["sk_extra"])
        self.assertNotIn(ids[0], index.live_ids())
        rollback(world, index, "snap-a")
        verify_rollback(world, index, "snap-a")
        self.assertEqual(set(index.live_ids()), set(ids))
        self.assertEqual(retrieve(world, index, "sk_extra.a"), [])
        self.assertFalse(world.flags.hygiene_enabled)
        self.assertFalse(world.flags.calibrated_scoring)
        self.assertTrue(world.incidents)
        self.assertTrue(world.screened)
        self.assertTrue(should_rollback(0.40, 0.50, 0.05))
        self.assertFalse(should_rollback(0.46, 0.50, 0.05))

    def test_flags_default_off_and_legacy_import_skips_sandbox(self):
        self.assertFalse(Flags().hygiene_enabled)
        self.assertFalse(Flags().calibrated_scoring)
        os.environ.pop("RECERTIA_HYGIENE_ENABLED", None)
        os.environ.pop("RECERTIA_CALIBRATED_SCORING", None)
        self.assertFalse(Flags.from_env().hygiene_enabled)
        world = _world()
        gate = HygieneGate(world, _ok_exec())
        legacy = import_trajectory(world, gate, _req("legacy"))
        self.assertEqual(legacy["path"], "legacy")
        self.assertEqual(world.sandbox_calls, 0)
        world.flags.hygiene_enabled = True
        gated = import_trajectory(world, gate, _req("gated"))
        self.assertTrue(gated.accepted)

    def test_review_queue_cannot_promote(self):
        world = _world()
        enqueue_review(world, "neg_1", "borderline")
        with self.assertRaises(PermissionError):
            promote_from_review(world, "neg_1")

    def test_turn_credit_decays_and_ignores_observations(self):
        from recertia.rsi.attribution import attribute

        credits = attribute(
            "scr_x",
            [TurnView("t0", 2, 100), TurnView("t1", 2, 100), TurnView("obs", 0, 500)],
            prioritized=1.0,
            gamma_turn=0.95,
        )
        self.assertEqual([c.turn_id for c in credits], ["t0", "t1"])
        self.assertAlmostEqual(sum(c.credit for c in credits), 1.0)
        self.assertGreater(credits[0].credit, credits[1].credit)

    def test_capacity_gate_blocks_phase1_when_headroom_is_low(self):
        allowed = capacity_report([1.0] * 50, free_slots=2, distill_slo_s=100.0)
        self.assertGreaterEqual(allowed["pool_headroom"], 1.5)
        self.assertTrue(allowed["phase1_allowed"])
        self.assertFalse(allowed["sampling_allowed"])
        blocked = capacity_report([1.0] * 50, free_slots=1, distill_slo_s=10.0)
        self.assertFalse(blocked["phase1_allowed"])
        with self.assertRaises(ValueError):
            capacity_report([1.0] * 10, free_slots=10, distill_slo_s=10.0)


if __name__ == "__main__":
    unittest.main()
