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
- [OpenAI-compatible gateways (OpenRouter)](architecture/openai-compat-gateways.md)
- [Measurement integrity](architecture/measurement-integrity.md)
- [Risk and governance](architecture/risk-and-governance.md)
- [Measurement and domain scope](architecture/measurement-and-scope.md)
- [Remaining work: implementation plan](architecture/remaining-work.md)
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

**Plans (partial engineering):**

- [External trajectories & computer-use goldens (2026-08-22)](plans/2026-08-22-external-trajectories-and-computer-use-goldens.md) — Phase 0 import + skill-free goldens and Phase 1 candidate distill landed; promotion-with-lift and a live computer backend remain open
- [2026-08-24 landing extract](plans/2026-08-24-todays-work-refactor.md) — dual-path extract after today's stack; landed, not a GA gate

Normative requirements are in the [specifications index](specifications.md).
Forward work is in the
[remaining-work implementation plan](architecture/remaining-work.md).
