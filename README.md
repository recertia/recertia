# Recertia

**Only memory that still works gets kept.**

Public homepage for this project is this repository:
[github.com/recertia/recertia](https://github.com/recertia/recertia).
Docs: [`docs/architecture.md`](docs/architecture.md) · changelog: [`CHANGELOG.md`](CHANGELOG.md) · contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md).

Recertia is a self-improving agent system for recurring work. It solves a task against locked, machine-checkable criteria, saves what worked as versioned memory, and measures whether that memory actually helps the next time.

It does not improve by updating model weights. It improves by updating how knowledge is stored, retrieved, and re-certified.

Related work: [SealedKeep](https://github.com/recertia/sealedkeep) keeps a code or experiment change only when a real evaluation metric improves. Recertia is the memory side of the same honesty rule of retrieve, solve, check, and keep only what still earns its place.

## What matters

**Retrieve before invent.**  
Solve always queries memory first. The agent is not allowed to skip straight to invention.

**Lock success before you solve.**  
Criteria are fixed at intake from a structured Goal. They must be able to fail. Natural language is optional context, not the contract.

**Version everything.**  
Skills, facts, and cases change by writing a new version with lineage. There is no silent in-place edit of “what we know.”

**Failures are knowledge.**  
Dead ends are stored and retrieved so the system does not fall into the same hole twice.

**Measure with a control arm.**  
Some runs suppress memory on purpose. “It got better” is only allowed when treatment beats control — not when a transcript sounds confident.

**Bound the library.**  
The active skill set is capped. Weak entries retire on measured contribution. Unbounded growth is not treated as progress.

**Do not self-edit the referee.**  
The agent may not change the machinery that measures or constrains it.

**Count what you dispatch.**  
Fan-in reconciles expected work against received work. Model-scored checks run in a fresh context so the solver cannot grade its own homework.

If you remember one sentence: progress is real only when a run without memory is part of the comparison.

## Prove it

The claim that matters is lift: same task class, with memory vs without.

```bash
pip install -e ".[dev]"
recertia lift --task-class <name> --trials 10
```

You should see success (and cost or step counts, when tracked) for the treatment arm and the control arm, plus a delta. The integrity ledger records both.

Lift is measured, not guaranteed. Some task classes will not improve. That result is still useful — it means the library did not earn its keep on that class.

Seed skills live under `skills/`. Golden evals live under `evals/`. Open empirical claims are tracked in [`docs/assumptions.md`](docs/assumptions.md).

## Try it

Python 3.11+.

```bash
pip install -e ".[dev]"

# preferred entry: structured Goal -> locked criteria
recertia run --goal goal.json

# inspect / resume / verify
recertia runs show <run_id>
recertia resume
recertia ledger verify

# library
recertia skills search <query>
recertia skills lint
recertia skills promote <skill_id>
```

HTTP API: `POST /v1/runs` (install with `pip install -e ".[api]"`). The core CLI
(`recertia --help`) does not need that extra. Issue keys with `recertia keys`.
Default create is still **sync** (`RECERTIA_API_DEFAULT_ASYNC` stays false).
`mode=async` enqueues; optional wait via `?wait=`, `X-Recertia-Wait`, or
`Prefer: wait` (cap 30s). `GET /v1/runs` is tenant-scoped keyset pagination
(`cursor` / `next_cursor`).

**Tenancy.** Runs and events are keyed by tenant. Memberships are `tenant_admin`
or `operator`. The CLI is the local operator (`platform_admin`). Export one
tenant (dry-run default):

```bash
recertia tenant export --tenant demo
recertia tenant export --tenant demo --execute --dest /tmp/demo-export
```

Drain the SQLite run queue (async API runs; sync `POST /v1/runs` stays inline):

```bash
recertia worker --root .recertia --once
```

Backup the `.recertia/` tree (takes the root lock; refuses if the API holds it):

```bash
recertia backup --root .recertia
recertia backup verify backups/recertia-….tar.gz
```

Ledger HMAC is optional (`RECERTIA_LEDGER_HMAC_KEY`). Unsigned ledgers still
verify until the first signed line:

```bash
recertia ledger verify --check-sig
recertia ledger rekey --dry-run
```

Operator details: [`CHANGELOG.md`](CHANGELOG.md) (Unreleased operator notes) and
[`docs/architecture/go-live.md`](docs/architecture/go-live.md). Deployment model:
[ADR-0021](docs/adr/0021-deployment-model.md).

**Execution backend.** Production solves default to an OCI container (`RECERTIA_EXECUTION_BACKEND=container`). Install Docker or Podman, then:

```bash
export RECERTIA_EXECUTION_BACKEND=container
python3 scripts/smoke_container.py
```

For local development only:

```bash
recertia run --goal goal.json --local-exec
```

**Models.** Unscripted solves need a real provider; the stub leaves the model unset on purpose so empty config fails loud. Anthropic, OpenRouter, and other OpenAI-compatible gateways are documented in [`docs/architecture/go-live.md`](docs/architecture/go-live.md). Keep keys out of the repo.

Offline jobs and retention:

```bash
recertia jobs run curator --dry-run
recertia gc --older-than-days 14
```

Policy defaults live in [`policy/default.json`](policy/default.json).

## How a run works

```text
                    ┌─────────────┐
                    │  Goal / API │
                    └──────┬──────┘
                           │
                           v
┌─────────────────────────────────────────────────────────┐
│  EXECUTION (one request)                                 │
│  intake → retrieve → plan → solve → validate             │
│              │                         │                 │
│              │                    pass / fail            │
│              │                         │                 │
│              │              distill or dead end          │
└──────────────┬─────────────────────────┬────────────────┘
               │                         │
               v                         v
┌─────────────────────────┐   ┌──────────────────────────┐
│  MEMORY (durable)        │   │  IMPROVEMENT (scheduled)  │
│  skills · facts · cases  │◀──│  curate · practice        │
│  utterances · policy     │──▶│  re-certify → quality gate│
│  versioned, reversible   │   └──────────────────────────┘
└───────────┬────────────┘
             │
             v
      ┌─────────────┐
      │ control arm │  same tasks, memory off → measure lift
      └─────────────┘
```

Compounding happens **across** runs, through durable memory that later runs read before inventing. Offline jobs (refine, evolve, practice, distill) reorganize and re-certify that memory on a schedule. Promotion always passes a quality gate.

Three planes stay separate on purpose:

| Plane | Lifetime | Role |
| --- | --- | --- |
| Execution | One request | Bounded graph walk; never learns in place |
| Memory | Durable | Versioned skills, facts, cases, utterances, policy |
| Improvement | Scheduled | Curate, practice, re-certify; gate before store |

Keeping them apart is what stops “self-improving” from meaning “one long process you have to trust.”

Primary input is a structured [`Goal`](contracts/goal.py) of outcomes and constraints, compiled to locked `TaskCriterion[]` at intake. Details: [`docs/specifications/goal-objects.md`](docs/specifications/goal-objects.md).

## This is not

- A fine-tuning or weight-training loop
- An unbounded autonomous agent
- A chat log you are expected to believe
- A promise that every task class will show positive lift
- An MIT-licensed free-for-commercial product

Validate it on your own workload before you rely on it.

## Status

Built and under test:

- Contracts-as-code in [`contracts/`](contracts) (source of truth) with generated [`schema/`](schema)
- Graph runtime with budgeted loops and explicit failure signals
- Plural memory planes, promotion gates, and control-arm lift measurement
- Container sandbox for production solves; `--local-exec` for development
- CLI (`recertia`; FastAPI is an optional `[api]` extra) and tenant-keyed HTTP API
- CI on every push

Research outcomes that are still open are listed in [`docs/assumptions.md`](docs/assumptions.md), separate from engineering acceptance gates.

## License

[PolyForm Noncommercial](LICENSE) 1.0.0 (`PolyForm-Noncommercial-1.0.0`). Personal use, research, experimentation, and use by many noncommercial organizations are permitted. This is **not** MIT. Commercial production use requires a separate license from the copyright holder.

Copyright (c) 2026 Robert Schmidt. See [`NOTICE`](NOTICE).

## More detail

| Doc | When you need it |
| --- | --- |
| [`docs/architecture2.md`](docs/architecture2.md) | All-in-one architecture + specifications (downloadable compilation) |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed |
| [`docs/brand/README.md`](docs/brand/README.md) | Logo and mark |
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | Planes, memory taxonomy, governance |
| [`docs/architecture/go-live.md`](docs/architecture/go-live.md) | Model credentials, jobs, retention |
| [`docs/architecture/container-sandbox.md`](docs/architecture/container-sandbox.md) | Docker/Podman setup and hardening |
| [`docs/specifications/goal-objects.md`](docs/specifications/goal-objects.md) | Goal as primary input |
| [`docs/adr/`](docs/adr/) | Architecture decision records |
| [`docs/plans/remediation/README.md`](docs/plans/remediation/README.md) | Multi-tenant remediation plan ([ADR-0021](docs/adr/0021-deployment-model.md)); specifications only |
| [`contracts/`](contracts) | Normative Pydantic models |
| [`docs/references.md`](docs/references.md) | Literature grounding |
| [`docs/assumptions.md`](docs/assumptions.md) | Empirical claims still under test |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, CI checks, how to send a change |
| [`SECURITY.md`](SECURITY.md) | Vulnerability reporting |
