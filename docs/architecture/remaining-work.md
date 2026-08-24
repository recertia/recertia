# Remaining work — implementation plan

Companion to [`assumptions.md`](../assumptions.md) (research outcomes).
The remaining *engineering* surface is this document.

M0–M9, console **C0–C4**, goal packs **GP0–GP2**, OpenRouter **OR0**, and the
roadmap-remaining CI gates are **shipped**. This document is the build order for what is
not. Sequencing follows the same rule as the archived M0–M9 plan: milestones are ordered
by what each one lets you **measure**, not by calendar appetite. Do not estimate calendar
time.

## 1. Guiding rules

1. **Harness before traffic, traffic before claims.** CI may prove a metric reports
   `"not established"` correctly. Only real `repo-chore` (then `research-synthesis`)
   traffic may move `a1` / `a2` / `a4` off `under evaluation` / `untested`
   ([`assumptions.md`](../assumptions.md) B7).
2. **Ops cadence is a product gate, not a docs footnote.** Operator-mode GA is four
   consecutive soak weeks plus a completed tabletop log. Code that already exists does
   not count as GA.
3. **Do not grow the graph.** Fifteen nodes remain T3. HEX, compress, learned rankers,
   and auto-advance stay off or deferred until their enablement predicates in §8 fire.
4. **Surface completion is parallel, not a substitute for measurement.** CLI/HTTP
   parity and OR polish must not delay probe cadence or soak.
5. **Hygiene is merge-blocking when it lies.** Stale "aspirational" tables and a
   README license line that contradicts `LICENSE` are defects, not style.

## 2. Inventory (what remains)

| ID | Kind | Item | Status |
| --- | --- | --- | --- |
| **RW-GA** | ops | Four consecutive soak weeks; tabletop log; baseline traffic metrics | open (soak log harness shipped) |
| **RW-M2** | engineering + ops | Scheduled probe + golden + ablation cadence; fill `MetricReport` holes | engineering shipped; live eval DB is ops |
| **RW-A** | research | Resolve `a1`, `a2`; instrument `a4` on live verifier versions | harness ready |
| **RW-LY** | engineering | `library_yield` and `retrieval_decay` on `MetricReport` | shipped (honest `unavailable` when sparse) |
| **RW-HEX** | gated engineering | Enable `practice_hex_search` / `curator_compress` | gated (JobRunner no-op without predicates) |
| **RW-PC** | engineering | Delete dual active-set path after Phase-2 measurement report | shipped |
| **RW-OR** | engineering | OpenRouter OR1–OR3 polish | OR0–OR3 shipped |
| **RW-SUR** | engineering | Remaining CLI/HTTP + unified error envelope | shipped (C5 UI still gated) |
| **RW-GP3** | deferred | Goal-pack auto-advance, DAG, `copy_forward` | explicit non-goal |
| **RW-C5** | gated | Multi-tenant console chrome | Phase-4 gate |
| **RW-TM** | ops + docs | Signed threat model; NIST AI RMF if tenant GA proceeds | single-operator §5 deltas accepted-with-owner; tenant signature open |
| **RW-HY** | hygiene | Spec/README drift (license, "aspirational" API table) | shipped |
| **RW-HCI** | engineering | Ye/Zhao high-confidence review fixes (honest faithfulness scorer, paired lift gap, exact applicability, writer, curator proposal log) | P0+P1+leftovers landed; `a9` under evaluation |

Deliberately out of scope for this year (unchanged from
[measurement-and-scope.md](measurement-and-scope.md) §18): fine-tuning, learned retrieval
ranker, RL on the policy plane, cross-tenant learning, self-authored tools, a third task
class before Phase 4, engine rewrite, auto-promotion past the golden gate.

## 3. Milestone map

```text
RW-HY  Spec and README drift                                      first (unblocks honesty)
RW-GA  Operator-GA closeout (ops on shipped code)                 Phase 1 remaining
RW-M2  Probe cadence + MetricReport completeness                  Phase 2 remaining
RW-A   Assumption status changes from traffic                     research, never a merge gate
RW-LY  library_yield + retrieval_decay                            Phase 3 remaining CI
RW-PC  Portfolio controller is the only path                      shipped
RW-OR  OR0–OR3 shipped (docs/cost honesty, robustness, presets)   shipped; parallel with RW-GA
RW-SUR Error envelope + remaining HTTP/CLI (+ distill HTTP twin)  shipped; not a GA gate
RW-HEX HEX / compress enablement                                  after a1 interval exists
RW-C5  Console C5 + tenant threat model                           Phase-4 gate only
```

```mermaid
flowchart LR
  HY[RW-HY hygiene] --> GA[RW-GA soak]
  GA --> M2[RW-M2 probe cadence]
  M2 --> A[RW-A a1/a2/a4]
  A --> LY[RW-LY yield/decay]
  LY --> HEX[RW-HEX optional]
  A --> C5{Phase-4 gate}
  C5 -->|pass| TM[C5 + threat model]
  C5 -->|fail| SO[single-operator two-domain]
  OR[RW-OR / RW-SUR] -.-> GA
```

## 4. RW-HY — Spec and README drift

**Shipped.** Living docs match `main` for the original drift items:

1. [`promotion-api-and-observability.md`](../specifications/promotion-api-and-observability.md)
   §9–10 lists shipped console C0–C4 routes. The aspirational table is C5 only.
2. README License matches `LICENSE` (PolyForm Noncommercial; explicitly not MIT).
3. This document is indexed from [`architecture.md`](../architecture.md),
   [`specifications.md`](../specifications.md), and the README documents table.

**Done when (engineering):** already met — `python3 scripts/check_cross_refs.py --check`
passes; the promotion-api table does not list a shipped route as unimplemented; README
license line matches `LICENSE`.

**Out of scope:** rewriting archived Q3 plans.

## 5. RW-GA — Operator-GA closeout

**Goal:** one operator can leave Recertia running on `repo-chore` with a real model,
container backend, and truthful spend. Engineering P0-1…P0-5 already landed; this
milestone is the **ops gate** (four consecutive soak weeks plus a tabletop log).

Shipped (do not rebuild): cost table + `ModelResponse.cost_usd`; command policy +
untrusted fetch delimiters; observe–act scratch loop; `RunManifest` pins;
`docker-compose.soak.yml` + `.github/workflows/weekly-ops.yml`;
[incident-tabletop.md](incident-tabletop.md); `recertia backup` / `recertia restore`;
`recertia tabletop`; `recertia canary` (synthetic + optional `--live`);
`scripts/backup_recertia.py`; `recertia soak record` / `recertia soak status`
([`ops/soak.py`](../../src/recertia/ops/soak.py)) — empty-eval-DB weeks are
written and **not counted**.

### Remaining work

| Item | Kind | Detail |
| --- | --- | --- |
| Soak log | ops | Four consecutive Monday `weekly-ops` runs (or `recertia soak record` on a host with live eval rows) with artifacts retained. Empty-eval-DB JSON is **not** a soak week; the harness refuses to count it. |
| Live traffic | ops | Operator runs against a real repo with `RECERTIA_EXECUTION_BACKEND=container` and a non-stub model. Record `reuse_rate`, `first_attempt_success`, `attempts_to_success`, `cost_per_solved_task`. |
| Tabletop log | ops | Run `recertia tabletop <run_id> --restore-from <backup.tar.gz>` and keep the JSON (date, run id, restore source, TTR, follow-up). Pass it to `recertia soak status --tabletop`. Tooling is shipped; the filled log is ops. |
| Postgres soak | ops | `scripts/soak_postgres.py --recertia-root .recertia` against compose Postgres **with** a snapshot if one exists. CI already migrates an empty DB and reports `recertia_snapshot=absent`. |
| Backup cron | ops | Nightly `python3 scripts/backup_recertia.py` (or volume snapshot); target RPO ≤ 24h. |
| Verifier split | ops | Distinct `RECERTIA_VERIFIER_MODEL_ID` on the soak host; `recertia canary --live` when that env is set. |

**Done when (ops gate, not CI):** four consecutive soak weeks green; tabletop log
exists; baseline metrics exist as numbers-or-unavailable on real traffic; zero open P0
rows from the principal review.

**Research recorded, not gated:** those baseline numbers. No lift claim this milestone.

**GA criteria:** the ops gate above. Code-complete ≠ GA.

## 6. RW-M2 — Probe cadence and MetricReport completeness

**Goal:** the weekly lift report is generated from **scheduled eval traffic**, not from
an empty checkout, and every §11 / §23 metric that is already on `MetricReport` is
either a number or an honest `unavailable` reason.

Shipped: `recertia metrics`, `scripts/weekly_metrics_report.py`, judge canary
fixtures, `recertia probes run`, `recertia canary` / `--live`, weekly-ops golden
suite + synthetic canary, `curation_gap` / `practice_conversion` /
`retirement_reversal_rate` / `active_cap_pressure` / `judge_false_pass_rate` /
`mean_composition_depth` / yield / decay fields.

### Remaining engineering

Engineering is shipped. Remaining is **ops**: weekly report against live
eval rows; `causal_lift` prints `"not established"` whenever the Wilson interval
spans zero; live verifier canary rates attributed to `provider × model_version`
without updating `a4` from CI.

**Done when (engineering):** CI synthetic observations produce `library_yield` and
`retrieval_decay` (or `unavailable`); probe runner fails CI if labelled precision
drops below the M1 floor of 0.7 on the committed probe set; weekly workflow uploads a
report that includes probe precision when an eval DB is present.

**Done when (ops):** the weekly report runs against live eval rows; `causal_lift`
prints `"not established"` whenever the Wilson interval spans zero.

**Research (RW-A, not a merge gate):** `a1` → `supported` or `refuted` with interval;
`a2` with observed certification-trial accumulation; `a4` → `under evaluation` with
the first live canary rates. Negative `a1` **halts** Phase-3 scope expansion.

## 7. RW-LY / RW-PC / RW-HEX — Library economics remainder

Shipped: trajectory emit, `ReplayPack` on Curator, `parallelise` / `serialise` /
`correction` jobs, retirement and composition fields on reports.

### RW-LY

Compute and export `library_yield` and `retrieval_decay` (see RW-M2). Dashboard /
`GET /v1/metrics/report` MUST pass `unavailable` through (PC-5 already locks this
pattern).

**Done when:** a Curator proposal in CI includes a replay pack **and** the weekly
report can show yield/decay or an honest hole; no silent zeros.

### RW-PC — Portfolio dual-path expiry

**Shipped.** The Phase-2 measurement report is
[`portfolio-measurement.md`](portfolio-measurement.md).
`_recompute_active_set_legacy` and `RECERTIA_PORTFOLIO_CONTROLLER` are deleted.
`recompute_active_set` ranks through `rank_skills` / `select_active` only.
`tests/unit/memory/test_portfolio_equivalence.py` expiry guard is green because
both are gone.

The two ranking differences versus the deleted legacy path (recency tiebreak,
integer version) match specs §24.1. Cap membership still does not bench.

### RW-HEX — Enablement (blocked)

`policy/default.json` keeps `practice_hex_search` and `curator_compress` **false**.
Enablement predicate (all required):

1. `practice_conversion` is a number (not `unavailable`) on a weekly report.
2. `causal_lift` for `repo-chore` has a Wilson interval that **excludes** zero, or
   `a1` is `refuted` and a design review explicitly re-opens HEX as a recovery
   experiment (still T2, still golden-gated).
3. `JobQuota` leftover admits HEX at `hex_share` without starving recertifier /
   curator retire.

Until then, jobs MUST refuse HEX/compress even if an operator flips the JSON locally
in production without an eval-compare note in the ledger.

## 8. RW-OR — OpenRouter polish (OR1–OR3)

**Shipped.** OR0–OR3 are on `main`. Inventory row RW-OR matches this section.
Configuring a gateway is still not evidence for `a1`.

| Milestone | Work | Status |
| --- | --- | --- |
| **OR1** | Docs gate: configuring a gateway MUST NOT be cited as evidence for `a1`. Unknown slugs use default rates; `cost_is_vendor_exact` is false; `unavailable`/notes must not say "vendor-exact". Locked by `test_og7_unknown_slug_is_not_vendor_exact`. | shipped; OG-7 |
| **OR2** | Default `max_tokens` via `RECERTIA_OPENAI_MAX_TOKENS` or EXTRA_BODY; map OpenRouter error JSON to `ProviderError`; tolerate list-shaped `message.content` text parts. Locked by OG-8…OG-10 tests. | shipped |
| **OR3** | Server-side allowlist of `provider:slug` for Pilot; unknown slug → 400; SPA never stores keys | shipped; PC-7 / OG-11 |

OR3 is optional for single-operator GA (env-level model is enough) but is implemented.

## 9. RW-SUR — Remaining HTTP and CLI

Console C0–C4 already expose runs list, SSE, cancel, skills, promote, proposals,
jobs, metrics, ledger, programs. The promotion-api "aspirational" list is C5 only
(RW-HY). Remaining HTTP/CLI:

| Surface | Status | Remaining |
| --- | --- | --- |
| `GET/POST /v1/reviews` | shipped | Alias of proposals until distill-review volume justifies a split |
| `POST /v1/evals/runs` | shipped | Golden set against a library snapshot |
| `POST /v1/trajectories/import` | shipped | HTTP twin of `recertia trajectory import`; never promotes |
| `POST /v1/trajectories/distill` | shipped | Candidate-only twin of `recertia trajectory distill`; never writes approved |
| `GET /v1/facts` · `/v1/cases` · `/v1/affordances` | shipped | Read non-procedural planes |
| `POST /v1/memory/query` | shipped | Federated retrieve debug |
| `GET /v1/policy` · `POST /v1/policy/proposals` | shipped | T2 change proposals; human approval + ledger |
| Unified error envelope | shipped | `{error:{code,message,run_id,retryable}}` on `/v1/*` HTTPException; `{detail}` remains on `/health` and 422 |
| CLI `skills list/show`, `review`, `eval run`, `memory query`, `facts`, `cases`, `proposals`, `policy` | shipped | Console twins; not a C0–C4 gate |

**Shipped.** Envelope tests on `POST /v1/runs` budget exhaustion; eval-run
endpoint writes the same `EvalObservation` rows as `recertia lift`; `/v1`
`HTTPException` uses the envelope; CLI twins exist. C5 UI remains gated.

## 10. RW-C5 / RW-TM — Phase 4 remainder

Do not start C5 UI until a Phase-4 go/defer
criteria hold. Remaining:

1. Threat-model re-review of principal-review §5 deltas — **accepted-with-owner** for
   single-operator ([threat-model-deltas.md](threat-model-deltas.md)); second-party
   signature still required before tenant GA.
2. NIST AI RMF Govern/Map — **open**, tenant-only.
3. Console C5: `GET /v1/me` multi-tenant switcher; cross-tenant leakage tests
   (planted-secret e2e already exists).
4. `research-synthesis` on **unchanged** runtime against real briefs (fixture is
   shipped; traffic is not).
5. Model-provider failover (P2-5): **deliberate-absence candidate** — decide at the
   gate with canary cost, do not build now.

Acceptable year-end: two-domain single-operator product.

## 11. Explicitly deferred (not remaining work)

| Item | Why |
| --- | --- |
| Goal-pack auto-advance, DAG, `copy_forward` | ADR-0014; `git_tip` is the continuity path |
| UNC / `\\?\` registered workspace roots | Windows v1 is drive-letter roots only |
| Provider token streaming into Pilot | SSE is run events, not tokens |
| Marking `a1`/`a2`/`a4` `supported` from CI | B7 |
| HEX/compress on by default | Enablement predicate in §7 |
| Third task class | Phase 4+ |
| Multi-tenant GA | Gate in production-readiness.md |

## 12. Operating cadence (unchanged)

Weekly metrics review, monthly assumptions register, quarterly threat-model refresh.
Status changes to `a1`–`a4` are **commits** to
[`assumptions.md`](../assumptions.md), not chat.

## 13. Success criteria for remaining work

1. Operator-mode GA (RW-GA) — unattended `repo-chore` with cost and soak.
2. `a1` and `a2` resolved with intervals, either direction (RW-A).
3. `a4` is a watched number per verifier model version.
4. `library_yield` / `retrieval_decay` are computed, not just defined (RW-LY).
5. Phase-4 go/defer recorded with a named owner.

## 14. Post-landing extract (not a GA gate)

The 2026-08-24 stack (MEA `#35`/`#37`/`#38`, trajectory Phase 0 `#39`, ADR-0018/0019 `#41`)
landed as sequential feature PRs. The dual-path extract (ingest `as_public_dict`,
`skill_task_class`, `ExactMatchTtl`, `hop_finished_attrs`, `not_established_detail`)
is engineering hygiene, not operator-GA and not a reason to skip Monday soak.

Plan:
[`docs/plans/2026-08-24-todays-work-refactor.md`](../plans/2026-08-24-todays-work-refactor.md).
Do not grow the graph. Do not enable idle offload until a golden-class RSS row exists.
Do not treat an empty-eval-DB `soak record` as a counted week.
