# Recertia Architecture: 9. Storage choices

## 9. Storage choices

| Concern | v1 | Upgrade path | Why |
| --- | --- | --- | --- |
| `SkillVersion`, facts, policy | JSON in git | Same, plus signed tags | Diffable, reviewable, revertible — this is the immutable half of ADR-0007's split |
| `SkillStatus`, `SkillStats`, cases | SQLite | Postgres | Runtime state, not reviewed artifacts — append-only event log and derived rows (ADR-0007), zero-ops start, identical SQL surface |
| Vector index (per plane) | `sqlite-vec` | `pgvector` | Co-located with metadata |
| Lexical index | SQLite FTS5 | Postgres `tsvector` | Same |
| Transcripts, snapshots | Content-addressed blobs on disk | Object storage | Large, write-once, dedupable |
| Checkpoints | SQLite rows | Postgres | Must survive process death |
| Integrity ledger | Append-only hash-chained table | Same, externally anchored | Tamper-evident provenance (§15.1) |

The v1 column lets one developer run everything locally with no services; the upgrade path is
a driver swap rather than a data model change.

Tenants are first-class on this tree: run state and events live at
`root/runs/<tenant>/`, blobs at `root/blobs/<tenant>/`. The control queue is
`root/control.db` (`recertia worker`). Backup (`recertia backup`) snapshots
SQLite with the online backup API, writes `MANIFEST.json` (sha256 per file),
and takes an exclusive root lock; the API holds that lock shared, so backup
refuses while the API is up. Restore also takes the lock.
[ADR-0021](../adr/0021-deployment-model.md) `multi_node` refuses SQLite; Postgres
RLS is not in this release.

### 9.1 What consulting memory costs

Recertia's premise is that the memory planes get bigger and runs get better because of it.
That only holds while the cost of consulting memory stays independent of how much it holds:
`retrieve` is mandatory on every task, so any per-record cost there is a tax on every run,
paid forever, and growing. The standing contract is therefore that **the online path may scan
an answer, never a plane**.

| Online cost | Shape | Measured |
| --- | --- | --- |
| Episodic dead ends and solved analogues | Flat in history size | 0.06ms at 0 and at 16k cases |
| Fact scoring | Linear, small coefficient | ~0.3µs per fact in scope |
| Fact cache validation | Flat in library size | One stat per fact *directory* per call |
| Procedural lexical + vector top-k | FTS5, then a scan of a cached embedding matrix | — |

Two of those deserve their reasoning recorded, because both were once the dominant cost of
the first step of every task:

- **Episodic lookups are bucketed, not scanned.** Asking for the most recent dead ends or
  solved analogues of a task class used to walk the index backwards until it collected three.
  That short-circuits only when history is dense with matches; a task class with no history
  walked every case ever recorded. Rows are now bucketed by `(kind, task_class)` when the
  index cache is built, so the answer is a slice of the tail.
- **Cache validation is tiered.** Detecting an arbitrary out-of-process edit to the fact tree
  costs one stat per fact, and doing that per call made validation four fifths of the retrieval
  it protected. Writes through the store invalidate directly; adds and removes bump a directory
  mtime, so the per-call gate stats only the fact directories; and the full per-file sweep that
  catches an external *in-place* edit runs on an interval. Only that last case is delayed, and
  only for edits made behind the store's back.

Fact scoring stays a scan because every fact carries a floor score, so the top-k can include
facts the query never matched — the ranking, not the implementation, is what makes it O(n). It
is cheap enough to leave alone at current library sizes; moving facts onto the FTS5 index the
procedural plane already uses is the lever if that changes, and it would need the ranking
change treated as a measurement change, not an optimisation.

Two costs remain linear by design, recorded here so they are budgeted rather than discovered:
a checkpoint carries the whole `RunState` and the state accumulates a route entry per hop, so
bytes written over a walk grow with the square of its length (475KB over 60 hops — chore-length
runs are fine, long multi-evolve walks are what would justify delta checkpoints); and both the
library fingerprint taken at every run start and the index snapshot id recomputed on every
`store` upsert are linear in library size (8ms and 0.9ms at 400 skills).

`scripts/bench_critical_path.py` measures all of this on demand, one dimension of state at a
time, with no model wired so engine and memory overhead are not hidden behind model latency.
The contracts themselves are pinned in `tests/unit/test_performance_regressions.py` as
assertions about *how much work happens* — files examined, indexes copied — rather than about
durations, so they hold on any machine and in CI.

## 10. Bounded loops and attempt isolation

### 10.1 Budgets

| Budget | Enforced at | Default |
| --- | --- | --- |
| `max_attempts` | `evolve → solve` | 4 |
| `max_tool_calls` | tool runtime | 200 |
| `max_tokens` | solver | task class default |
| `max_wall_clock_s` | solver, per attempt | 900 |
| `max_cost_usd` | solver + tool runtime | task class default |
| `max_branches` | `fan_out` | 3 |
| `max_parallel_steps` | step scheduler in `solve` | 8 |
| `claim_timeout_s` | resource claim acquisition | 60 |
| `max_versions_written` | `budget_excess` + `distill` / `review` / `store` ([ADR-0017](../adr/0017-version-write-budget.md)) | 2 |

Exhausting any budget routes to `classify_failure` then `record_dead_end`, never to another
`solve`. No-progress detection short-circuits when two consecutive attempts produce an
identical result vector: the same failure twice means the current strategy is exhausted, not
unlucky.

A budget is only as good as the meter behind it, so `RunState.spent` has exactly one writer
module: `recertia.nodes.attempt`. `AttemptMeter` charges attempt-scoped dimensions
(attempts, tools, tokens, wall clock, cost). `charge_version_write` charges
`versions_written` at the `store` hop ([ADR-0017](../adr/0017-version-write-budget.md)).
Each `solve` path opens a meter, charges what it uses, and closes through the meter's
outcome helpers, which means no exit can record an attempt while omitting a dimension —
the failure mode that left wall clock uncharged and therefore unenforceable.

Charging is per attempt, while the model client, tool runtime, and claim scheduler count
cumulatively for the whole run. `RuntimeWindow` reports the difference between two reads of
those counters; reading them directly charges an attempt for every attempt before it. The
window is measured inside `ctx.op_once` and persisted alongside the operation result, so a
resumed run charges replayed work exactly once rather than losing it. A boundary test parses
the AST to keep both rules enforced rather than merely documented.

Budgets are also *allocated*, not just capped. The policy plane holds an escalation ladder —
start on a cheap model tier, escalate on specific failure classes — because spending
frontier-model budget on a task that a cheap tier solves is the most common way cost per
solved task fails to improve even as success rates do.

### 10.2 Attempt isolation and compensation

`solve` mutates a workspace, so retrying naively means attempt 2 starts from attempt 1's
mess — a bug that produces uninterpretable failures and poisons distillation. Therefore:

- Each attempt runs against a workspace snapshot taken before it starts.
- `evolve` restores the snapshot before routing back to `solve`, so every attempt starts
  from a known state, and the diff between attempts is attributable.
- Fan-out branches get disjoint workspace clones.
- Steps running in the same wave share the attempt's workspace, so the wave — not the step —
  is the unit of rollback. Restoring half a wave would leave a state no attempt ever produced.
- Irreversible external side effects (`external` tools) are gated by approval and recorded
  with a compensating action where one exists; a skill whose steps include an uncompensable
  external effect cannot run in `portfolio` or `shadow` mode.
