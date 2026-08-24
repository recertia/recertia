# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **MEA graph wiring** — intake binds an `AuditedTaskState` sidecar when three-layer
  activation is true (`policy.mea_enabled` + `Goal.mea_opt_in` +
  `Task.execution_strategy=="mea"`); validate is the fresh-context auditor (CAS);
  the trajectory emitter writes `audited_state_delta` on accept; the ledger notes
  `mea_activation_fallback` only when MEA was requested but incomplete. Default
  single-request path is unchanged (no sidecar, no ledger note, no extra events).
  No new graph nodes. `AuditedTaskState` and `TrajectoryImport` are generated
  into `schema/`. Skill-free golden
  `evals/golden/mea/synthetic-multiphase/` runs under `recertia eval run
  --task-class mea`; observations are eval-firewalled and cannot enter
  `causal_lift`. Not on the repo-chore promotion gate.

- **External trajectories Phase 0** — `recertia trajectory import` validates
  the existing `TrajectoryImport` contract (incomplete provenance / empty
  environment rejected), persists append-only under
  `{runs_root}/runs/{tenant}/imports/`,
  and writes an episodic case. Never promotes. Skill-free computer-use
  goldens `evals/golden/{bug_reproduction,playtest_operator,docs_auditor}/`
  run under `recertia eval run --task-class …`; observations are
  eval-firewalled and cannot enter `causal_lift`. Not on the repo-chore
  promotion gate. Phase 1 distill, optional `external_computer`, and the
  ADR remain open. Plan:
  [`docs/plans/2026-08-22-external-trajectories-and-computer-use-goldens.md`](docs/plans/2026-08-22-external-trajectories-and-computer-use-goldens.md).

- **Variance-aware causal lift** — `RunVariance` on `CausalLiftResult` (std-dev, best,
  worst, best–worst gap); `low_run_count` when independent trials are below
  `Policy.min_independent_runs` (default 5). `EvalStore` exposes Bernoulli vectors
  and per-snapshot rates. `recertia lift` prints the gap and refuses established
  language below the floor (Ye et al. 2026).
- **Faithfulness interventions** — eval-only `empty` / `corrupt` / `irrelevant` /
  `filler` transformers (`recertia.evals.interventions`) and
  `recertia faithfulness`. Trajectory Jaccard + normalized edit-distance, pairwise
  by fixture; zero-trial arms are unscored (`score` is None). `--trials N` writes
  tagged `faithfulness:*` observations through `IntervenedSkillStore` +
  `Retriever.bundle_hook`. Those rows cannot enter lift or contribution samples.
  T3, import-forbidden from nodes/jobs (Zhao et al. 2026). Production omits the hook.
  `Retriever.bundle_hook` is constructor-only (read-only after init). Curator
  persists specificity proposals to `proposals.jsonl` so weekly runs do not
  re-flag the same skill.

- **Applicability gate** — environment (run `ToolRuntime` when present), exact
  locked-criterion match, contagion structural hash (embedding cosine is advisory).
  Distiller injects the environment model and criterion summary. Ledger action
  `applicability_reject`. Distill routes a failing draft to `one_off`. Promote
  requires a non-judge certification criterion.

- **Specificity lint** — `SPEC` / `VAGUE` on drafts; `require_failure_modes` on the
  authoring prior (`ap-2026.08.1`). Warnings only for already-approved seeds.
  Missing preconditions are a SPEC finding. Curator emits specificity-review
  proposals against the active set and does not `lint_reject` approved seeds.

- Ledger actions `lift_report`, `faithfulness_report`, `applicability_reject`.
- **arXiv paper ingestion (Miner)** — `src/recertia/jobs/arxiv.py` Atom client;

  `mine_from_arxiv` proposals with `curation=mined_from_paper`; CLI `--arxiv-id` /
  `--arxiv-query` / `--arxiv-max` on `recertia jobs run mine`; optional `--submit`
  for candidate drafts only. Docs: `docs/architecture/arxiv-ingest.md`.
  Tests: `tests/unit/jobs/test_arxiv_ingest.py`.
- **Curation enum** — `mined_from_paper` added to `contracts/common.py`.
- `recertia soak record` / `recertia soak status` — empty-eval-DB weeks are
  recorded and not counted. Does not declare GA (RW-GA harness).
- Phase-2 portfolio measurement report
  (`docs/architecture/portfolio-measurement.md`).
- ADR-0016 (interval-bounded retirement) and ADR-0017 (version-write budget).
- `charge_version_write` — sole writer of `spent.versions_written`.
- `assemble_bundle` shared by retrieve and the debug query. Affordance flake
  thresholds live on `RetrievalConfig`.
- `GraphOrchestrator(on_finalize=...)` callback. Eval recording moved to the
  composition root so `recertia.graph` no longer imports `EvalStore`.

### Changed

- ADR-0016 non-goals: RW-PC shipped (dual active-set path is gone).
  and `RECERTIA_PORTFOLIO_CONTROLLER` are gone (RW-PC /
  [`portfolio-measurement.md`](docs/architecture/portfolio-measurement.md)).
- Retirement benches on `interval_high < −τ`, not the point estimate (ADR-0016).
  A missing interval cannot retire.
- `budget_excess` includes `versions_written`. Distill / review refuse a write
  that would exceed `max_versions_written`; `store` is the hard stop (ADR-0017).
- Extract Method on the walk: `solve` is a strategy switch into sibling modules,
  `distill` is named honesty gates, `Retriever.search` is stage calls, `_execute`
  is hop / route / snapshot / checkpoint.
- Split `SearchCapability` from `IndexMaintenance`. The retrieve node cannot
  rebuild or upsert. Debug `federated_query` refuses a stale index instead of
  rebuilding it.
- One `retirement_decision` predicate. `propose_retirements` and
  `maybe_bench_on_contribution` are adapters. The Curator job now applies
  proposals; `recompute_active_set` still does not bench.
- `estimate_contribution(..., has_required_non_judge)` is required. Judge-only
  samples produce `estimate is None`.

### Notes

- Paper candidates are retrieval stubs. Promotion still requires the golden gate;
  this path does not claim lift.
- External-trajectory Phase 0 (import CLI + computer-use goldens) must not delay
  RW-GA soak weeks or probe cadence. Distill/promotion is still Phase 1.

## [0.1.0] - 2026-08-15

First public preview. Engineering through M0–M9 is on `main`. Operator-mode GA
(soak weeks, tabletop log, live `repo-chore` metrics) is still open; do not read
this version as production-ready.

### Added

- Contracts-as-code (`contracts/`) with generated JSON Schema (`schema/`)
- Graph runtime, plural memory planes, golden-gated promotion, control-arm lift
- CLI (`recertia`) and optional FastAPI console (Pilot / Tower / Ops)
- Container execution backend (Docker/Podman); `--local-exec` for development
- Seed skills and golden evals under `skills/` and `evals/`
- PolyForm Noncommercial license (`LICENSE`, `NOTICE`), `SECURITY.md`, and `CONTRIBUTING.md`
- Canonical GitHub identity locked to `github.com/recertia/recertia` (clone URL, package
  metadata, and contributor guide). `pyproject.toml` is parsed in CI so a duplicate
  `license` key cannot break install again.

### Notes

Research outcomes `a1`–`a4` stay in [`docs/assumptions.md`](docs/assumptions.md)
until real traffic produces intervals. Remaining ops gates:
[`docs/architecture/remaining-work.md`](docs/architecture/remaining-work.md).
