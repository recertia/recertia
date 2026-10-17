# Recertia Architecture

> This index replaces the former monolithic architecture document. Existing links remain valid; use the topic files below for direct references.
>
> All-in-one download (architecture + specifications + ADRs): [`architecture2.md`](architecture2.md).

- [Overview: purpose, graph rationale, planes, and memory](architecture/overview.md)
- [Task plane](architecture/task-plane.md)
- [Skill composition](architecture/skill-composition.md)
- [Library lifecycle](architecture/library-lifecycle.md)
- [Portfolio measurement (Phase-2 / RW-PC)](architecture/portfolio-measurement.md)
- [Improvement plane](architecture/improvement-plane.md)
- [arXiv paper ingestion (Miner)](architecture/arxiv-ingest.md)
- [Operations: storage, budgets, and attempt isolation](architecture/operations.md)
- [Container sandbox: Docker/Podman setup and hardening](architecture/container-sandbox.md)
- [Single-user go-live: models, tools, jobs, retention](architecture/go-live.md)
- [ADR-0021: multi-tenant deployment model](adr/0021-deployment-model.md) (`single_node` / `multi_node`)
- [OpenAI-compatible gateways (OpenRouter)](architecture/openai-compat-gateways.md)
- [Measurement integrity](architecture/measurement-integrity.md)
- [Risk and governance](architecture/risk-and-governance.md)
- [Measurement and domain scope](architecture/measurement-and-scope.md)
- [Remaining work: implementation plan](architecture/remaining-work.md)
- [Store receipts remaining work (RW-SR / ADR-0020)](architecture/remaining-work-sr.md)
- [Incident tabletop (operator GA)](architecture/incident-tabletop.md)
- [Threat-model deltas (principal review §5, single-operator)](architecture/threat-model-deltas.md)
- [Product console architecture](architecture/product-console.md)
- [Goal packs (migration programs)](architecture/goal-packs.md)

**Shipped (engineering on `main`):**

- [High-confidence measurement items (Ye/Zhao)](plans/2026-08-high-confidence-items-implementation.md)
- MEA Audited Task-State (Phases 0–3 scaffolding + graph wiring + lift golden; default off)
- ADR-0018 systems residency (hop gauges, read-only caches, idle offload default-off, prefix-tree view, `recertia systems`). Fixture RSS is not a 4.6× claim.
- ADR-0019 rewrite: `recertia trajectory distill` / `POST /v1/trajectories/distill` (candidate only), `POST /v1/trajectories/import`, pending proposal, gated `external_computer`. Names bind to `contracts.policy.COMPUTER_USE_TASK_CLASSES` (`bug_reproduction`, `playtest_operator`, `docs_auditor`) and `TrajectoryImport`. Snake on Goals/goldens; kebab only on `SkillVersion.task_class` via `skill_task_class`. Promotion-with-lift and a live computer backend remain open.
- 2026-08-24 landing extract — single owner per ingest/distill/cache/systems/hop-gauge seam; prefix tree stays a view; no 16th node.
- ADR-0021 control-plane slice — tenant-keyed runs/events, memberships, durable SQLite queue (`recertia worker`), backup `MANIFEST.json`, optional ledger HMAC, HMAC API keys, console cookie v2 / CSRF / SameSite=Strict. Postgres RLS, KMS, Firecracker, JWKS signature verify, crypto-shred, and async-by-default are **not** in this release.

**Plans (partial engineering):**

- [External trajectories & computer-use goldens (2026-08-22)](plans/2026-08-22-external-trajectories-and-computer-use-goldens.md) — Phase 0 import + skill-free goldens and Phase 1 candidate distill landed; promotion-with-lift and a live computer backend remain open
- [2026-08-24 landing extract](plans/2026-08-24-todays-work-refactor.md) — dual-path extract after today's stack; landed, not a GA gate
- [Recuris record (2026-08-26)](plans/2026-08-26-recuris.md) — Yu et al. arXiv:2608.24876; closest published sibling; cite, do not reshape T3
- [Store receipts (2026-09-23)](plans/2026-09-23-store-receipts.md) — ADR-0020 Proposed; P0 contract only; `backup --receipts` stays off until P4 after one counted soak week compares tar vs receipt restore
- [Multi-tenant remediation](plans/remediation/README.md) — [ADR-0021](adr/0021-deployment-model.md) accepted. First operator-visible slice is shipped on the SQLite tree (see Unreleased in [`CHANGELOG.md`](../CHANGELOG.md)); Postgres RLS, KMS, Firecracker, and the async-by-default flip remain later work

Normative requirements are in the [specifications index](specifications.md).
Forward work is in the
[remaining-work implementation plan](architecture/remaining-work.md).
Literature grounding is in [references.md](references.md).
