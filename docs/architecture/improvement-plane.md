# Recertia Architecture: 8. Improvement plane

## 8. Improvement plane

Five scheduled jobs. Each proposes changes through the same review and promotion path as any
run — no job writes `approved` state directly. See
[ADR-0004](../adr/0004-offline-improvement-plane.md).

### 8.1 Miner — cold-start bootstrap

An empty library means every early user pays full price and the system looks worse than a
plain agent exactly when first impressions form. The Miner attacks that by distilling
candidate skills and facts from artefacts that already exist: git history, merged pull
requests, CI configuration, runbooks, and docs. Mined candidates enter as `draft` and must
pass validation like anything else — but they arrive with real evidence attached, because a
merged PR is a solved task with a review already on it.

This job is more than a convenience. Mined skills are `mined_from_human_artifact`, and the
measured gap between human-curated and self-generated skills
([`references.md`](../references.md) §1.1) makes human-authored history the single most promising
source of early library quality. If that gap replicates in our domain, the Miner is the primary
mechanism and self-distillation is the supplement — the reverse of the original assumption.

### 8.2 Curator — capacity and entropy control

Retrieval precision decays as a library grows, and the surveyed literature puts that decay in the
moderate regime of tens to hundreds of skills, with lifecycle management "largely neglected" as
the field-wide bottleneck ([`references.md`](../references.md) §1.1, §1.5). Curation is therefore the
load-bearing subsystem, not housekeeping.

The Curator proposes, in rough order of measured value: **retiring** skills with negative
contribution past the evidence floor (§7.2), **extracting** shared sub-procedures into child
skills (§6), **splitting** overloaded skills whose criteria fail in uncorrelated clusters,
**tightening** preconditions that produced wrong retrievals, **merging** near-duplicates, and
**compacting** version chains. Every proposal is a diff, gated by the golden-set regression run.

Two proposals act on step graphs rather than skill content. **Parallelise** removes an
`input_bindings` entry whose bound input was unused across repeated runs — and whose steps'
claims do not overlap. **Serialise** does the reverse, adding a binding or widening a claim
after repeated merge failures or resource conflicts on the same wave. This is the loop that
makes concurrency a learned property: the distiller writes bindings from a single transcript,
and the Curator relaxes or tightens them once many runs have shown which consumptions were real.

Deduplication sits late in that list deliberately: with a consistent authoring prior in place,
explicit deduplication was found to be largely subsumed by the prior itself
([`references.md`](../references.md) §1.2), so it earns effort only after retirement and abstraction
are working.

**Compress** (ADR-0015) is a later Curator proposal kind: unit-level (existing steps / `uses`
children), replay-first LOO, one golden at the end. Default off until a weekly lift interval
exists. It does not run Shapley on live traffic.

Jobs share a weekly `JobQuota`. Recertifier and retirement go first; Practice HEX and compress
spend leftover tokens only (HEX ≤ 25% of the weekly cap).

### 8.3 Practice — curriculum at the frontier

Waiting for user tasks means learning only what traffic happens to cover. Practice generates
tasks aimed at the frontier of competence: task classes with high failure rates, classes with
≥3 recorded one-offs, skills with stale certification, and near-miss variations of tasks that
just barely passed. Selection targets the band where success probability is neither near 1
nor near 0, because that is where an attempt carries information. Practice runs are marked as
such, are budgeted separately, and their results never count toward user-facing metrics.

Eligible failure clusters (incremental `FailureClusterRow`, written on `record_dead_end`)
outrank one-off success practice when `n_runs ≥ 3` and `n_sessions ≥ 2`. The operator
`practice` job reads `eligible` first; `--one-off` is an explicit override. SkillHEX-shaped
search, when enabled, runs only here: it may publish a `PatchTemplate`. User-facing `evolve`
applies a published template as the single class repair and does not search
([ADR-0015](../adr/0015-improvement-plane-search.md)). HEX stays off until
`practice_conversion` is a measured number.

### 8.4 Recertifier — drift defence

Skills rot without anyone touching them: tools upgrade, APIs change, the model version
changes underneath. The Recertifier re-runs skills against their golden fixtures on a
schedule and on triggers — model upgrade, tool version change, child invalidation, and the
lineage-revoke queue — and moves failures to `needs_recert` or `quarantined` (§13). Revoke is
never applied inline on the task plane.

A `SkillStatus` transition *into* `quarantined` enqueues the version and its authoring
sources. `recertia jobs run recertify` (and the console job of the same name) drains that
queue through the lineage point index (`lineage.idx.json`; JSONL is WAL only) and is
write-capped by `JobQuota.max_status_writes_per_tick`.

### 8.5 Correction miner — improving the learner

When a reviewer edits a draft before approving, the diff is the single highest-quality signal
the system receives: a human demonstrating what a good skill looks like. The original design
stored the decision and discarded the edit. The Correction miner clusters these diffs into
recurring correction patterns and proposes updates to distiller guidance and criteria
templates. This is where the system improves *how it learns*, not just what it knows — and it
is bounded by §14.

### 8.6 Policy document and job quota

The checked-in T2 document is [`policy/default.json`](../../policy/default.json)
(`RECERTIA_POLICY_PATH` overrides). It holds `ImprovementFlags`, `ImprovementLimits`, and
the `JobQuota` **caps**. Flags MUST NOT grow `contracts.graph.NODES`.

Weekly spend (`tokens_spent`, HEX counters) is reserved then settled in
`job_quota.sqlite` next to the job runs root. `{runs_root}/jobs/job_quota.json`
(or `.recertia/jobs/job_quota.json`) is the operator export. ISO-week rollover
in UTC resets spend. The policy file is never rewritten to record consumption.

Admit order: recertifier → curator retire → fail-cluster author → practice band →
practice HEX (≤25% leftover) → compress. HEX and compress remain default-off.
