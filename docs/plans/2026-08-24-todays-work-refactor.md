# Agent prompt — 2026-08-24 landing extract

**Date:** 2026-08-24 (Monday)  
**Status:** extract landed (characterization tests + seam owners). Not a GA gate.  
**Audience:** a Recertia Cloud Agent (or equivalent) executing one structural pass.  
**Goal pack:** [`2026-08-24-todays-work-refactor.program.json`](2026-08-24-todays-work-refactor.program.json) (`MigrationProgram`, ADR-0014).  
**Baseline:** `origin/main` after `#35`–`#41` (MEA scaffolding + graph wiring + lift golden, paper scoring `#36`, trajectory Phase 0 `#39`, config honesty `#40`, ADR-0018/0019 `#41`). Fetch `origin/main` before you branch.

Paste the boxed prompt below into a new Cloud Agent. The rest of this file is the same contract with file-level seams and related work.

---

## Paste into a Cloud Agent

```text
Refactor the 2026-08-24 landing on recertia/recertia. This is extract + honesty, not a feature PR.

Context (today, Monday 2026-08-24)
Main just stacked: MEA Audited Task-State (default off, no new graph nodes), recertia trajectory
import + skill-free computer-use goldens, then ADR-0018 systems residency + rewritten ADR-0019
(candidate distill, HTTP import twin, pending proposal, gated external_computer, JobQuota
computer-use share, recertia systems --brief). Those PRs are merged. Do not rebase closed #33.
Open PR #31 (arxiv distill / PDF facts) is out of scope — do not collide with it.

Start from current origin/main. Branch cursor/<descriptive>-3b41.

What to do (in order)
1. Characterization. Add or extend tests that lock CURRENT behaviour before you move code:
   import rejects incomplete provenance / empty environment; distill authors a candidate only
   (never approved) and refuses true-noop steps/criteria; goldens are eval-firewalled and off
   the repo-chore promotion gate; MEA sidecar only when policy.mea_enabled + Goal.mea_opt_in +
   Task.execution_strategy=="mea"; idle offload default off
   (state_management.idle_offload_enabled); caches are read-only (writes never cached; index
   rebuild flushes); external_computer refuses unless isolation.allow_external_computer and a
   non-empty allow-list, and still opens no standing VM; recertia systems / --brief print
   "not established" rather than a 4.6× or lift claim from fixtures. Prefer extending
   tests/unit/test_systems_phase0.py, tests/unit/test_trajectory_import_cli.py,
   tests/unit/ops/test_mea_systems.py, tests/e2e/test_mea_graph_wiring.py,
   tests/e2e/test_computer_use_goldens.py over a parallel suite.

2. Honesty. docs/architecture.md still says Phase 1 distill / ADR are open. They are not: candidate
   distill + ADR-0018/0019 are on main. Promotion-with-lift and a live computer backend remain
   open. Align CHANGELOG Phase 0 "ADR remain open" leftover with the ADR-0019 Unreleased bullets.
   Bind every name to shipped code: contracts.policy.COMPUTER_USE_TASK_CLASSES
   (bug_reproduction, playtest_operator, docs_auditor), TrajectoryImport,
   recertia trajectory import|distill, POST /v1/trajectories/import. Snake on Goals/goldens;
   kebab only on SkillVersion.task_class, mapped in one helper (skill_task_class). If docs
   disagree with code, fix docs.

3. Structural extract (by seam, not by rewrite). Dual paths landed in a hurry — collapse them:
   - Systems: recertia.ops.systems (AgentSysBench six-property snapshot) vs
     recertia.ops.mea_systems (MEA stuck/coverage) vs recertia.ops.operator_brief
     (stuck / lift-by-class / redundancy). Keep three projections if the data is different;
     share snapshot_from_events / stuck detection / "not established" formatting. CLI
     recertia systems and recertia task-state must not duplicate JSON shape logic.
   - Caches: recertia.retrieval.cache vs recertia.solver.result_cache. One read-only
     memo policy (writes never cached; index rebuild / mutating tool flushes). Two wrappers
     are fine; two eviction stories are not.
   - Distill: recertia.distill.imported vs recertia.distill.success. Imported path must call
     the same hygiene / no-op / non-judge certification gates as success distill. Keep the
     snake→kebab map in one function (skill_task_class).
   - CLI vs HTTP: recertia trajectory import and POST /v1/trajectories/import must share
     ingest_trajectory. Distill stays candidate-only on both surfaces if you add an HTTP twin.
   - Engine: hop telemetry in recertia.graph.engine (component_class, rss_bytes, workdir_bytes,
     idle_gap_ms) is Extract Method into recertia.telemetry / recertia.ops.systems. Do not add
     a 16th graph node.
   - Prefix tree (recertia.trajectory.prefix_tree) stays a view over existing JSONL, not a
     second stream.

4. Behaviour lock. Same tests green. ruff, mypy, schema --check, examples --check, cross-refs,
   milestone-deps, assumptions hygiene, security_review --check, pytest -q. If you touch
   contracts/, regenerate schemas. If you touch architecture topic files, regenerate
   docs/architecture2.md.

Related work you must not ignore
- RW-GA soak is a Monday ops gate (weekly-ops / recertia soak record on a host with live eval
  rows). Empty-eval-DB JSON is not a soak week. This refactor must not delay soak, must not
  declare GA, and must not count fixture RSS as a 4.6× claim (see docs/plans/2026-08-systems-baseline.md).
- Do not enable practice_hex_search / curator_compress, do not grow the graph, do not mark
  assumptions a1/a2/a4 supported from CI, do not put computer-use goldens on the repo-chore
  promotion gate, do not turn MEA on by default, do not wire a live allow-listed computer.
- Do not merge or rewrite open #31. Do not resurrect Policy.external_trajectory_import or
  kebab golden directories from closed #33.

Constraints
- Freeze: contracts/ (unless a name is genuinely duplicated), policy/default.json semantics
  (flags may move, defaults may not), evals/golden/**, skills/**, LICENSE.
- Match house style of #25 / #27: Extract Method, single predicate, delete the dual path.
- One PR. Descriptive commit messages. No drive-by refactors outside the seams above.

Done when
- Dual ingest/distill/systems/cache logic has a single owner module per seam.
- architecture.md / remaining-work.md / CHANGELOG describe shipped vs open without lying.
- Characterization tests still fail if someone promotes from import, enables offload by
  default, or caches a write.
- CI checks listed above are green.
```

---

## Why this exists

Recertia’s own rule is **Goal packs for large refactors**, not a mega-Goal and not a
prompt-only card (`docs/architecture/goal-packs.md`, ADR-0014). Today’s merged stack is
~4k insertions across MEA, import, distill-candidate, systems telemetry, caches, and
offload — landed as sequential feature PRs. The extract is one day’s engineering: make
the seams look like `#25` (walk extract) and `#27` (portfolio controller is the only
active-set path), and stop the architecture index from advertising work that `#41`
already shipped.

This is **not** operator-GA. RW-GA remains four consecutive Monday soak weeks with live
eval rows ([remaining-work.md](../architecture/remaining-work.md) §5).

## Inventory of today’s landings

| PR | What shipped | Extract seam |
| --- | --- | --- |
| `#35` `#37` `#38` | MEA sidecar + validate auditor + skill-free `mea` golden | `ops/mea_systems.py` vs `ops/systems.py`; default-off activation |
| `#36` | Paper scoring / five-day arXiv scan | docs only; do not reopen |
| `#39` | `recertia trajectory import`, skill-free computer-use goldens | CLI vs `import_store.ingest_trajectory` |
| `#40` | Phase 0 config JSON is proposed, not `Policy` | already honesty; keep it |
| `#41` | ADR-0018 telemetry/caches/offload/prefix view; ADR-0019 candidate distill, HTTP import, gated `external_computer`, quota share, `systems --brief` | caches, distill gates, HTTP twin, engine telemetry, operator brief |

Still **open on purpose:** promotion with control-arm lift on a computer-use class; a live
allow-listed computer backend; golden-class RSS (offload stays off until then); AgentSysBench
plan phases that treat the prefix tree as a production control loop.

## Related work (in the prompt, not extra features)

1. **Monday soak (RW-GA).** Ops, not this PR. Do not treat `recertia soak record` on an empty
   eval DB as a week. Do not block soak on extract completeness.
2. **Index honesty.** `docs/architecture.md` still says Phase 1 distill / ADR are open.
   Candidate distill and both ADRs are on `main` as of `#41`.
3. **Name binding.** CHANGELOG, ADR-0019, CLI help, and `COMPUTER_USE_TASK_CLASSES` have
   drifted in prose. The executable names are the contract + CLI. Pick those; fix prose.
4. **Open `#31`.** Paper distill / optional PDF extract. Orthogonal. No overlapping file
   grabs in `jobs/arxiv.py` unless you are only importing a helper.
5. **Closed `#33`.** Duplicate `TrajectoryImport`, kebab goldens, `Policy.external_trajectory_import`.
   Dead. ADR-0019 exists so nobody rebuilds it.
6. **Compose heuristic.** `console_compose.py` already decomposes “refactor the whole” into
   inventory → structural move → behaviour lock. This pack is that heuristic made durable.

## Non-goals

- New graph nodes, Bot fleet, persistent-VM default, seed-skill promotion from import.
- Enabling HEX/compress, marking research assumptions `supported` from CI.
- Consumer/GTM surfaces, multi-tenant C5 chrome, NIST RMF.
- Rewriting archived Q3 plans.

## Verification

```bash
export PATH="$HOME/.local/bin:$PATH"
ruff check contracts/ src/ scripts/ tests/ conftest.py
mypy contracts/ src/recertia/
python3 scripts/generate_schemas.py --check
python3 scripts/export_examples.py --check
python3 scripts/check_cross_refs.py --check
python3 scripts/check_milestone_deps.py --check
python3 scripts/check_assumptions_hygiene.py --check
python3 scripts/security_review.py --check
python3 scripts/generate_architecture2.py --check   # if architecture topic files changed
pytest -q
```

Docker is not in the Cloud environment. Use `--local-exec` / existing pytest fixtures.
Do not add a container-smoke job.

## Rollback

Revert the extract PR. Behaviour is unchanged if characterization tests were honest.
Any change that promotes from import, enables offload by default, or caches writes is
a rollback trigger regardless of tests.
