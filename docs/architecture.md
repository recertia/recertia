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

**Plans (design, not yet engineering):**

- [High-confidence measurement items (Ye/Zhao)](plans/2026-08-high-confidence-items-implementation.md)
- [External trajectories & computer-use goldens (2026-08-22)](plans/2026-08-22-external-trajectories-and-computer-use-goldens.md)

Normative requirements are in the [specifications index](specifications.md).
Forward work is in the
[remaining-work implementation plan](architecture/remaining-work.md).
