"""Persistent eval observations and per-task-class control baselines (M4)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from contracts.eval import BinomialSample, ControlBaseline, EvalObservation, RunResolution
from contracts.run import RunState
from contracts.stats import RetrievalAblationEffect
from recertia.persistence.db import open_db

_UNRESOLVED_EXITS = frozenset({124, 126})
_UNRESOLVED_REASONS = {
    124: "required criterion timed out (exit 124)",
    126: "required criterion unavailable (exit 126)",
}


class ObservationError(ValueError):
    """A run cannot be represented as durable evaluation evidence."""


def _first_required_exit_code(state: RunState) -> int | None:
    results = {result.criterion_id: result for result in state.results}
    for criterion in state.criteria:
        if not criterion.is_required:
            continue
        result = results.get(criterion.id)
        if result is None or result.exit_code is None:
            continue
        return int(result.exit_code)
    return None


def classify_run_resolution(state: RunState) -> tuple[RunResolution, int | None, str]:
    """Derive the recording-only resolution from a locked terminal ``RunState``.

    Validation already stores criterion ``exit_code`` on ``RunState.results``
    (timeout → 124, ``ExecutionUnavailable`` / sandbox refusal → 126). ``Terminal``
    has no distinct value for those cases, so this reads the results vector instead
    of extending the terminal enum.

    ``terminal="error"`` is ``invalid`` and keeps the required criterion exit
    code. Exit 124 or 126 on a required criterion is ``unresolved``. Everything
    else is ``decided``. Callers must not feed this into ``arm_counts``, lift,
    gates, or promotion.
    """

    if state.terminal == "error":
        return "invalid", _first_required_exit_code(state), "terminal=error"
    results = {result.criterion_id: result for result in state.results}
    for criterion in state.criteria:
        if not criterion.is_required:
            continue
        result = results.get(criterion.id)
        if result is None or result.exit_code not in _UNRESOLVED_EXITS:
            continue
        code = int(result.exit_code)
        return "unresolved", code, _UNRESOLVED_REASONS[code]
    return "decided", None, "decided"


def reported_resolution(stored: str | None) -> str:
    """Map a stored resolution column to the reported label.

    Pre-REC-011 rows are NULL and are reported as ``legacy``. They are never
    coerced to ``decided``.
    """

    if stored is None:
        return "legacy"
    return stored


class EvalStore:
    """SQLite store keyed by snapshot so baselines survive across library versions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = open_db(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def close(self) -> None:
        self._conn.close()

    def _init(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    run_id TEXT PRIMARY KEY,
                    task_class TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    model_version TEXT,
                    first_attempt_success INTEGER NOT NULL,
                    predicted_success REAL,
                    terminal TEXT,
                    fixture_id TEXT,
                    is_eval_fixture INTEGER NOT NULL,
                    strategy TEXT,
                    attempt_no INTEGER,
                    cost_usd REAL,
                    abstention_confirmed INTEGER,
                    skill_id TEXT,
                    skill_version INTEGER,
                    suppressed_skill_id TEXT,
                    suppressed_skill_version INTEGER,
                    valid_non_judge_evidence INTEGER NOT NULL DEFAULT 0,
                    evidence_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    resolution TEXT,
                    exit_code INTEGER,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_class TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    model_version TEXT,
                    successes INTEGER NOT NULL,
                    trials INTEGER NOT NULL,
                    interval_json TEXT,
                    created_at TEXT NOT NULL,
                    report_id TEXT,
                    UNIQUE(task_class, snapshot_id, report_id)
                );
                CREATE TABLE IF NOT EXISTS retrieval_ablation_effects (
                    task_class TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_obs_snap ON observations(snapshot_id, task_class);
                CREATE INDEX IF NOT EXISTS idx_obs_skill ON observations(skill_id, skill_version, task_class);
                CREATE INDEX IF NOT EXISTS idx_obs_suppressed
                    ON observations(suppressed_skill_id, suppressed_skill_version, task_class, arm);
                CREATE INDEX IF NOT EXISTS idx_obs_arm ON observations(task_class, arm, is_eval_fixture);
                CREATE INDEX IF NOT EXISTS idx_base_class ON baselines(task_class, created_at);
                CREATE TABLE IF NOT EXISTS probe_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_class TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    precision_at_3 REAL NOT NULL,
                    skill_count INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_probe_class ON probe_snapshots(task_class, recorded_at);
                """
            )
            existing = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(observations)")
            }
            migrations = {
                "strategy": "TEXT",
                "attempt_no": "INTEGER",
                "cost_usd": "REAL",
                "abstention_confirmed": "INTEGER",
                "skill_id": "TEXT",
                "skill_version": "INTEGER",
                "suppressed_skill_id": "TEXT",
                "suppressed_skill_version": "INTEGER",
                "valid_non_judge_evidence": "INTEGER NOT NULL DEFAULT 0",
                "evidence_hash": "TEXT NOT NULL DEFAULT ''",
                "tenant_id": "TEXT",
                "resolution": "TEXT",
                "exit_code": "INTEGER",
                "reason": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE observations ADD COLUMN {column} {definition}")

    def append_run(
        self,
        state: RunState,
        *,
        strategy_override: str | None = None,
        force_eval_fixture: bool = False,
        tenant_id: str | None = None,
    ) -> EvalObservation:
        """Append an observation derived only from a completed, locked ``RunState``.

        The run ID is a primary key and is never replaced.  The canonical run payload hash is
        retained with the derived fields so callers cannot rewrite an observation after the fact.
        ``strategy_override`` / ``force_eval_fixture`` let the faithfulness writer tag rows
        without forging an observation.
        """

        if state.terminal is None:
            raise ObservationError("cannot record an observation for a non-terminal run")
        if state.criteria_locked_at is None or state.manifest.criteria_hash is None:
            raise ObservationError("cannot record a run whose criteria were not locked")
        if not state.task.task_class:
            raise ObservationError("cannot record a run without a task_class")
        if not state.manifest.index_snapshot_id:
            raise ObservationError("cannot record a run without index_snapshot_id")
        evidence = state.model_dump(mode="json")
        evidence_hash = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        required_non_judge = {
            criterion.id
            for criterion in state.criteria
            if criterion.is_required and criterion.kind != "judge"
        }
        results = {
            result.criterion_id: result
            for result in state.results
            if not result.errored
        }
        valid_non_judge_evidence = bool(required_non_judge) and all(
            criterion_id in results for criterion_id in required_non_judge
        )
        is_fixture = bool(state.task.is_eval_fixture or force_eval_fixture)
        fixture_id = state.task.task_id if is_fixture else None
        resolution, resolution_exit_code, resolution_reason = classify_run_resolution(state)
        obs = EvalObservation(
            run_id=state.run_id,
            task_class=state.task.task_class,
            arm=state.arm,
            snapshot_id=state.manifest.index_snapshot_id,
            model_version=state.manifest.model_version,
            first_attempt_success=state.terminal == "solved" and state.attempt_no == 1,
            predicted_success=state.predicted_success,
            terminal=state.terminal,
            fixture_id=fixture_id,
            is_eval_fixture=is_fixture,
            recorded_at=datetime.now(timezone.utc),
            strategy=strategy_override if strategy_override is not None else state.strategy,
            attempt_no=state.attempt_no,
            cost_usd=state.spent.cost_usd,
            abstention_confirmed=state.terminal == "abstained" and state.failure is not None,
            skill_id=state.chosen.skill_id if state.chosen else None,
            skill_version=state.chosen.version if state.chosen else None,
            suppressed_skill_id=(
                state.suppressed_skill.skill_id if state.suppressed_skill else None
            ),
            suppressed_skill_version=(
                state.suppressed_skill.version if state.suppressed_skill else None
            ),
            valid_non_judge_evidence=valid_non_judge_evidence,
            evidence_hash=evidence_hash,
            resolution=resolution,
            exit_code=resolution_exit_code,
            reason=resolution_reason,
        )
        self._append_observation(obs, tenant_id=tenant_id)
        return obs

    def record_observation(self, obs: EvalObservation) -> None:
        """Reject caller-authored observations; use :meth:`append_run` instead."""

        del obs
        raise ObservationError("observations must be append_run-derived from a locked RunState")

    def _append_observation(self, obs: EvalObservation, *, tenant_id: str | None = None) -> None:
        with self._lock, self._conn:
            try:
                self._conn.execute(
                """
                INSERT INTO observations (
                    run_id, task_class, arm, snapshot_id, model_version,
                    first_attempt_success, predicted_success, terminal, fixture_id,
                    is_eval_fixture, strategy, attempt_no, cost_usd, abstention_confirmed,
                    skill_id, skill_version, suppressed_skill_id, suppressed_skill_version,
                    valid_non_judge_evidence, evidence_hash, recorded_at, payload, tenant_id,
                    resolution, exit_code, reason
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    obs.run_id,
                    obs.task_class,
                    obs.arm,
                    obs.snapshot_id,
                    obs.model_version,
                    int(obs.first_attempt_success),
                    obs.predicted_success,
                    obs.terminal,
                    obs.fixture_id,
                    int(obs.is_eval_fixture),
                    obs.strategy,
                    obs.attempt_no,
                    obs.cost_usd,
                    None if obs.abstention_confirmed is None else int(obs.abstention_confirmed),
                    obs.skill_id,
                    obs.skill_version,
                    obs.suppressed_skill_id,
                    obs.suppressed_skill_version,
                    int(obs.valid_non_judge_evidence),
                    obs.evidence_hash,
                    obs.recorded_at.isoformat(),
                    obs.model_dump_json(),
                    tenant_id,
                    obs.resolution,
                    obs.exit_code,
                    obs.reason,
                ),
            )
            except sqlite3.IntegrityError as exc:
                raise ObservationError(f"run {obs.run_id!r} already has an immutable observation") from exc

    def write_baseline(self, baseline: ControlBaseline) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO baselines (
                    task_class, snapshot_id, model_version, successes, trials,
                    interval_json, created_at, report_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    baseline.task_class,
                    baseline.snapshot_id,
                    baseline.model_version,
                    baseline.control.successes,
                    baseline.control.trials,
                    baseline.interval.model_dump_json() if baseline.interval else None,
                    baseline.created_at.isoformat(),
                    baseline.report_id,
                ),
            )

    def baselines_for(self, task_class: str) -> list[ControlBaseline]:
        rows = self._conn.execute(
            """
            SELECT * FROM baselines
            WHERE task_class = ?
            ORDER BY created_at ASC, id ASC
            """,
            (task_class,),
        ).fetchall()
        return [self._row_to_baseline(r) for r in rows]

    def latest_baseline(self, task_class: str) -> ControlBaseline | None:
        rows = self.baselines_for(task_class)
        return rows[-1] if rows else None

    def arm_counts(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> dict[str, BinomialSample]:
        sql = """
            SELECT arm,
                   SUM(first_attempt_success) AS successes,
                   COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND is_eval_fixture = 0
              AND (strategy IS NULL OR strategy NOT LIKE 'faithfulness:%')
        """
        params: list[object] = [task_class]
        if snapshot_id is not None:
            sql += " AND snapshot_id = ?"
            params.append(snapshot_id)
        sql += " GROUP BY arm"
        out: dict[str, BinomialSample] = {}
        for row in self._conn.execute(sql, params):
            out[row["arm"]] = BinomialSample(
                successes=int(row["successes"] or 0), trials=int(row["trials"] or 0)
            )
        return out

    def success_vectors(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> dict[str, list[float]]:
        """Per-observation Bernoulli outcomes (1.0/0.0) per arm, in recorded order.

        Faithfulness-tagged rows (``strategy`` prefixed with ``faithfulness:``) and eval
        fixtures are excluded so they cannot be mistaken for lift data.
        """

        sql = """
            SELECT arm, first_attempt_success, strategy
            FROM observations
            WHERE task_class = ? AND is_eval_fixture = 0
        """
        params: list[object] = [task_class]
        if snapshot_id is not None:
            sql += " AND snapshot_id = ?"
            params.append(snapshot_id)
        sql += " ORDER BY recorded_at ASC, run_id ASC"
        out: dict[str, list[float]] = {}
        for row in self._conn.execute(sql, params):
            strategy = row["strategy"] or ""
            if str(strategy).startswith("faithfulness:"):
                continue
            out.setdefault(row["arm"], []).append(1.0 if row["first_attempt_success"] else 0.0)
        return out

    def snapshot_rate_map(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> dict[str, dict[str, float]]:
        """arm → snapshot_id → rate, excluding fixtures and faithfulness-tagged rows."""

        sql = """
            SELECT arm, snapshot_id,
                   SUM(first_attempt_success) AS successes,
                   COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND is_eval_fixture = 0
              AND (strategy IS NULL OR strategy NOT LIKE 'faithfulness:%')
        """
        params: list[object] = [task_class]
        if snapshot_id is not None:
            sql += " AND snapshot_id = ?"
            params.append(snapshot_id)
        sql += " GROUP BY arm, snapshot_id ORDER BY snapshot_id ASC"
        out: dict[str, dict[str, float]] = {}
        for row in self._conn.execute(sql, params):
            trials = int(row["trials"] or 0)
            if trials <= 0:
                continue
            out.setdefault(row["arm"], {})[row["snapshot_id"]] = (
                int(row["successes"] or 0) / trials
            )
        return out

    def snapshot_rates(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> dict[str, list[float]]:
        """Per-snapshot success rates per arm (one rate per snapshot with trials)."""

        mapped = self.snapshot_rate_map(task_class=task_class, snapshot_id=snapshot_id)
        return {
            arm: [mapped[arm][sid] for sid in sorted(mapped[arm])]
            for arm in mapped
        }

    def arm_rate_series(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> tuple[list[float], list[float]]:
        """Rates used for per-arm variance: per-snapshot if ≥2 snapshots, else Bernoulli 0/1."""

        treatment, control, _paired, _kind = self.variance_inputs(
            task_class=task_class, snapshot_id=snapshot_id
        )
        return treatment, control

    def variance_inputs(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> tuple[list[float], list[float], list[float], str]:
        """Treatment rates, control rates, paired-on-snapshot lifts, and series kind.

        Kind is ``snapshot`` when either arm has ≥2 snapshots, else ``bernoulli``.
        Paired lifts use the intersection of snapshot ids only.
        """

        mapped = self.snapshot_rate_map(task_class=task_class, snapshot_id=snapshot_id)
        t_map = mapped.get("treatment", {})
        c_map = mapped.get("control", {})
        if len(t_map) >= 2 or len(c_map) >= 2:
            t_rates = [t_map[sid] for sid in sorted(t_map)]
            c_rates = [c_map[sid] for sid in sorted(c_map)]
            paired = [t_map[sid] - c_map[sid] for sid in sorted(set(t_map) & set(c_map))]
            return t_rates, c_rates, paired, "snapshot"
        vectors = self.success_vectors(task_class=task_class, snapshot_id=snapshot_id)
        return vectors.get("treatment", []), vectors.get("control", []), [], "bernoulli"

    def contribution_samples(
        self, *, skill_id: str, version: int, task_class: str, snapshot_id: str | None = None
    ) -> tuple[BinomialSample, BinomialSample]:
        """Return randomized shadow and suppression samples for one skill."""

        extra = " AND (strategy IS NULL OR strategy NOT LIKE 'faithfulness:%')"
        suffix = extra + (" AND snapshot_id = ?" if snapshot_id is not None else "")
        params: list[object] = [task_class, skill_id, version]
        if snapshot_id is not None:
            params.append(snapshot_id)
        shadow = self._sample(
            """
            SELECT SUM(first_attempt_success) AS successes, COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND skill_id = ? AND skill_version = ?
              AND arm = 'shadow' AND is_eval_fixture = 0
              AND valid_non_judge_evidence = 1
            """
            + suffix,
            params,
        )
        suppression_params: list[object] = [task_class, skill_id, version]
        if snapshot_id is not None:
            suppression_params.append(snapshot_id)
        suppression = self._sample(
            """
            SELECT SUM(first_attempt_success) AS successes, COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND suppressed_skill_id = ? AND suppressed_skill_version = ?
              AND arm = 'control' AND is_eval_fixture = 0
              AND valid_non_judge_evidence = 1
            """
            + suffix,
            suppression_params,
        )
        return shadow, suppression

    def contribution_samples_bulk(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> dict[tuple[str, int], tuple[BinomialSample, BinomialSample]]:
        """Shadow + suppression samples for every evidenced skill, two scans total.

        Equivalent to calling :meth:`contribution_samples` for each skill, without the
        per-skill round trips; skills with no evidence are absent from the result.
        """

        extra = " AND (strategy IS NULL OR strategy NOT LIKE 'faithfulness:%')"
        suffix = extra + (" AND snapshot_id = ?" if snapshot_id is not None else "")
        params: list[object] = [task_class]
        if snapshot_id is not None:
            params.append(snapshot_id)
        shadow_rows = self._conn.execute(
            """
            SELECT skill_id, skill_version,
                   SUM(first_attempt_success) AS successes, COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND arm = 'shadow' AND is_eval_fixture = 0
              AND valid_non_judge_evidence = 1
              AND skill_id IS NOT NULL AND skill_version IS NOT NULL
            """
            + suffix
            + " GROUP BY skill_id, skill_version",
            params,
        ).fetchall()
        suppression_rows = self._conn.execute(
            """
            SELECT suppressed_skill_id, suppressed_skill_version,
                   SUM(first_attempt_success) AS successes, COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND arm = 'control' AND is_eval_fixture = 0
              AND valid_non_judge_evidence = 1
              AND suppressed_skill_id IS NOT NULL AND suppressed_skill_version IS NOT NULL
            """
            + suffix
            + " GROUP BY suppressed_skill_id, suppressed_skill_version",
            list(params),
        ).fetchall()

        empty = BinomialSample(successes=0, trials=0)
        out: dict[tuple[str, int], list[BinomialSample]] = {}
        for row in shadow_rows:
            out.setdefault((row["skill_id"], int(row["skill_version"])), [empty, empty])[0] = (
                BinomialSample(
                    successes=int(row["successes"] or 0), trials=int(row["trials"] or 0)
                )
            )
        for row in suppression_rows:
            out.setdefault(
                (row["suppressed_skill_id"], int(row["suppressed_skill_version"])),
                [empty, empty],
            )[1] = BinomialSample(
                successes=int(row["successes"] or 0), trials=int(row["trials"] or 0)
            )
        return {key: (pair[0], pair[1]) for key, pair in out.items()}

    def record_probe_snapshot(
        self,
        *,
        task_class: str,
        snapshot_id: str,
        precision_at_3: float,
        skill_count: int,
        payload: dict | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        """Persist a labelled probe run for ``retrieval_precision_at_3`` / decay."""

        at = recorded_at or datetime.now(timezone.utc)
        body = json.dumps(payload or {}, sort_keys=True)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO probe_snapshots (
                    task_class, snapshot_id, precision_at_3, skill_count, recorded_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_class, snapshot_id, precision_at_3, skill_count, at.isoformat(), body),
            )

    def list_probe_snapshots(
        self, *, task_class: str, limit: int = 8
    ) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT snapshot_id, precision_at_3, skill_count, recorded_at, payload
            FROM probe_snapshots
            WHERE task_class = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            (task_class, limit),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "snapshot_id": row["snapshot_id"],
                    "precision_at_3": float(row["precision_at_3"]),
                    "skill_count": int(row["skill_count"]),
                    "recorded_at": row["recorded_at"],
                    "payload": json.loads(row["payload"] or "{}"),
                }
            )
        return out

    def field_failure_streaks(self) -> dict[tuple[str, int], int]:
        """Trailing treatment-arm failure counts per applied skill (non-fixture).

        ``solved`` and ``abstained`` break the streak. ``practice`` / ``shadow`` /
        ``control`` arms and eval fixtures are ignored — those are not the operator's
        live mix.
        """

        rows = self._conn.execute(
            """
            SELECT skill_id, skill_version, terminal, recorded_at
            FROM observations
            WHERE arm = 'treatment' AND is_eval_fixture = 0
              AND skill_id IS NOT NULL AND skill_version IS NOT NULL
            ORDER BY skill_id, skill_version, recorded_at DESC, run_id DESC
            """
        ).fetchall()
        streaks: dict[tuple[str, int], int] = {}
        broken: set[tuple[str, int]] = set()
        for row in rows:
            key = (str(row["skill_id"]), int(row["skill_version"]))
            if key in broken:
                continue
            terminal = row["terminal"]
            if terminal in {"solved", "abstained"}:
                broken.add(key)
                streaks.setdefault(key, 0)
                continue
            if terminal in {"unsolved", "rejected", "error"}:
                streaks[key] = streaks.get(key, 0) + 1
            else:
                broken.add(key)
                streaks.setdefault(key, 0)
        return streaks

    def retrieval_ablation_samples(
        self, *, task_class: str, snapshot_id: str | None = None
    ) -> tuple[BinomialSample, BinomialSample]:
        """Return class-level retrieval-enabled and retrieval-suppressed samples."""

        suffix = " AND snapshot_id = ?" if snapshot_id is not None else ""
        params: list[object] = [task_class]
        if snapshot_id is not None:
            params.append(snapshot_id)
        enabled = self._sample(
            """
            SELECT SUM(first_attempt_success) AS successes, COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND arm = 'treatment' AND is_eval_fixture = 0
              AND valid_non_judge_evidence = 1
            """
            + suffix,
            params,
        )
        suppressed = self._sample(
            """
            SELECT SUM(first_attempt_success) AS successes, COUNT(*) AS trials
            FROM observations
            WHERE task_class = ? AND arm = 'control' AND is_eval_fixture = 0
              AND valid_non_judge_evidence = 1
            """
            + suffix,
            params,
        )
        return enabled, suppressed

    def write_retrieval_ablation(self, effect: RetrievalAblationEffect) -> None:
        """Persist the one class-level retrieval effect, separate from skill stats."""

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO retrieval_ablation_effects (task_class, payload, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    effect.task_class,
                    effect.model_dump_json(),
                    effect.last_evaluated_at.isoformat()
                    if effect.last_evaluated_at
                    else datetime.now(timezone.utc).isoformat(),
                ),
            )

    def retrieval_ablation(self, task_class: str) -> RetrievalAblationEffect | None:
        row = self._conn.execute(
            "SELECT payload FROM retrieval_ablation_effects WHERE task_class = ?", (task_class,)
        ).fetchone()
        return RetrievalAblationEffect.model_validate_json(row["payload"]) if row else None

    def metric_rows(
        self,
        *,
        task_class: str | None = None,
        snapshot_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """Return stored, run-derived rows in the shape consumed by metric aggregation."""

        return [
            observation.model_dump(mode="json")
            for observation in self.list_observations(
                task_class=task_class, snapshot_id=snapshot_id, tenant_id=tenant_id
            )
        ]

    def _sample(self, sql: str, params: list[object]) -> BinomialSample:
        row = self._conn.execute(sql, params).fetchone()
        return BinomialSample(
            successes=int((row["successes"] if row else 0) or 0),
            trials=int((row["trials"] if row else 0) or 0),
        )

    def list_observations(
        self,
        *,
        task_class: str | None = None,
        snapshot_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[EvalObservation]:
        clauses: list[str] = []
        params: list[object] = []
        if task_class is not None:
            clauses.append("task_class = ?")
            params.append(task_class)
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        if tenant_id is not None:
            clauses.append("(tenant_id IS NULL OR tenant_id = ?)")
            params.append(tenant_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT payload FROM observations{where} ORDER BY recorded_at ASC", params
        ).fetchall()
        return [EvalObservation.model_validate_json(r["payload"]) for r in rows]

    @staticmethod
    def _row_to_baseline(row: sqlite3.Row) -> ControlBaseline:
        from contracts.eval import ConfidenceInterval

        interval = None
        if row["interval_json"]:
            interval = ConfidenceInterval.model_validate_json(row["interval_json"])
        return ControlBaseline(
            task_class=row["task_class"],
            snapshot_id=row["snapshot_id"],
            model_version=row["model_version"],
            control=BinomialSample(successes=row["successes"], trials=row["trials"]),
            interval=interval,
            created_at=datetime.fromisoformat(row["created_at"]),
            report_id=row["report_id"],
        )


def baseline_from_control(
    *,
    task_class: str,
    snapshot_id: str,
    control: BinomialSample,
    model_version: str | None = None,
    report_id: str | None = None,
) -> ControlBaseline:
    from recertia.evals.statistics import wilson_interval

    return ControlBaseline(
        task_class=task_class,
        snapshot_id=snapshot_id,
        model_version=model_version,
        control=control,
        interval=wilson_interval(control.successes, control.trials),
        created_at=datetime.now(timezone.utc),
        report_id=report_id,
    )
