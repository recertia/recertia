# Product console architecture

Companion to [ADR-0012](../adr/0012-product-console-surfaces.md), normative contracts in
[`../specifications/product-console.md`](../specifications/product-console.md).

## 1. Purpose

Give operators and reviewers a **user-friendly control surface** for Recertia without
weakening the headless runtime’s measurement and promotion invariants. The console replaces
hand-edited Goal JSON and CLI archaeology for day-to-day use; it does not replace the graph,
the golden gate, or the offline improvement plane.

## 2. Posture

| Principle | Implication |
| --- | --- |
| Headless core | Browser never runs nodes; all execution goes through API + workers |
| Two tempos | **Pilot** (run) vs **Tower** (library / jobs / review) |
| Git as source of truth for library | `skills/`, `facts/`, `policy/` remain reviewable artifacts |
| Buy observability | Grafana / OTel for deep dashboards; console shows operational summaries |
| Single-operator first | Control-plane tenancy is shipped; C5 console chrome stays gated |
| Honest unavailable | Metrics panels reuse `MetricReport.unavailable` reasons (B7) |

## 3. Surfaces

```text
┌─────────────────────────────────────────────────────────────┐
│  Console (SPA or server-rendered app)                       │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │ Pilot        │  │ Tower        │  │ Ops               │ │
│  │ Goal form    │  │ Skills       │  │ Metrics / canary  │ │
│  │ Runs browser │  │ Proposals    │  │ Quotas / keys*    │ │
│  │ Live stream  │  │ Practice     │  │ Incident links    │ │
│  │ Resume/Cancel│  │ Curator      │  │                   │ │
│  └──────┬───────┘  └──────┬───────┘  └─────────┬─────────┘ │
└─────────┼─────────────────┼────────────────────┼───────────┘
          │                 │                    │
          ▼                 ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│  Console BFF / expanded /v1 API  +  event stream (SSE)      │
└─────────┬─────────────────┬────────────────────┬───────────┘
          │                 │                    │
          ▼                 ▼                    ▼
   Graph worker        SkillStore /        EvalStore /
   + checkpoints       Review queue        Telemetry
                       + jobs
```

\* Key *management* for service accounts may stay CLI/`admin` for v1; the console shows
quota and identity, not necessarily self-serve key minting.

### 3.1 Pilot (run tempo)

- **Compose (sub-mode)** — intent → `POST /v1/goals/suggest` draft (model or heuristic) →
  human select/edit → apply to form. Drafts are never locked (ADR-0003 / ADR-0010). Large
  briefs may return a **Goal pack** / decomposition instead of one mega-Goal. Stress warnings
  flag vacuous commands and missing `must_not_modify`.
- **Run (sub-mode)** — form fields compile to `Goal` JSON (`file_exists`, `file_contains`,
  `command`, constraints) via **Preview** (`compile_goal`) then submit. Templates for common
  `repo-chore` chores.
- **Programs board** — durable migration programs (`/v1/programs`): ordered steps,
  freeze/mutate hints, per-step preview/run/bind (see [goal-packs.md](goal-packs.md)). Distinct
  from Tower **ReplayPack** evidence.
- **Workdir picker** — sandbox (default) or **registered workspace** (allowlisted Windows
  host root + optional subpath). Never accept raw absolute `workdir` on create-run; see
  [registered-workspaces.md](../specifications/registered-workspaces.md).
- **Runs browser** — list/filter by tenant, task class, terminal, time; open detail.
- **Run detail** — route log, spend, manifest pins, failure class, links to transcript and
  trajectory.
- **Live view** (after C2) — SSE of node/tool/terminal events; cancel at node boundary.

### 3.2 Tower (library tempo)

- **Skill library browser** — list by lifecycle / task class; show version, status, stats,
  contribution, certification freshness.
- **Proposal inbox** — durable queue of job and distill proposals (`mine`, `curate`,
  `practice`, `parallelise`, `serialise`, `correction`, …) with approve / reject /
  request-changes.
- **Practice panel** — one-off clusters, curriculum artifacts, conversion metric, trigger
  Practice job under budget.
- **Curator panel** — active-set pressure, replay packs attached to proposals, dry-run vs
  submit, retirement evidence floor display.
- **Promote action** — enqueues golden-gated promotion; shows pass/fail task ids; never
  silent `approved`.

### 3.3 Ops

- Embed or link `MetricReport` / weekly lift / canary false-pass rate.
- Quota snapshot per tenant (existing `QuotaStore`).
- Deep links to incident tabletop and production-readiness docs.

## 4. Backend shape

### 4.1 Sync vs async runs

| Mode | When | Behaviour |
| --- | --- | --- |
| Sync (default) | Short chores, CLI/API scripts | `POST /v1/runs` blocks until terminal. `RECERTIA_API_DEFAULT_ASYNC` stays false |
| Async | Pilot live UX, `mode=async` | Enqueues on `control.db`, returns `202` + `run_id` unless a wait is set (`?wait=`, `X-Recertia-Wait`, `Prefer: wait`, cap 30s). Drain with the embedded API worker or `recertia worker`. Clients subscribe to `/v1/runs/{id}/events` |

Both modes share `GraphOrchestrator`, checkpoints, manifests, and ledgers. Async is an
admission and transport change, not a second graph.

### 4.2 Event stream

Source of truth for live UI: append-only **run events** derived from:

1. Existing telemetry required events (`run.*`, `node.*`, `tool.*`, …)
2. Trajectory events (ADR-0011)
3. Thin UI events (`review.queued`, `job.finished`, `promote.finished`)

Transport: **SSE** first (simple, HTTP/2 friendly, good enough for single-operator).
WebSocket only if bidirectional control proves necessary beyond REST cancel.

### 4.3 Durable proposal queue

Today jobs print `Proposal` JSON. The console requires:

- Persist proposals under `{runs_root}/proposals/<id>.json` (or SQLite table) with status
  `pending | approved | rejected | superseded`
- Link to optional Git PR URL when the Git-native path is used
- Every human decision appends a ledger entry

Jobs still MUST NOT write `approved` skills (ADR-0004).

Skill detail MUST label **Authoring** (`Provenance.source_*`, curation, derivation) separately
from **Applications** (`SkillStats.apply_diversity`). The API `identity` object is that
split — not a third store ([ADR-0015](../adr/0015-improvement-plane-search.md)).

### 4.4 AuthN / AuthZ

| Actor | Mechanism | Console role |
| --- | --- | --- |
| Human operator / reviewer | OIDC (or SSO proxy injecting identity); default auth is off | Session cookie v2 (`HttpOnly`, `SameSite=Strict`, `Secure` outside `dev`); CSRF double-submit (`recertia_csrf` + `X-Recertia-CSRF`); Origin must match Host when present; roles `operator`, `reviewer`, `admin`. OIDC nonce + `jti`; JWKS signature verify is not shipped |
| Automation / CI | Existing `X-API-Key` scopes (HMAC-SHA256) | Unchanged |
| Browser → API | BFF holds session; exchanges for scoped service credentials or passes user JWT with audience=recertia-api | Never embed long-lived API keys in SPA. Sessions are ignored when `RECERTIA_CONSOLE_AUTH=off` |

Tenant binding: human identity maps to one or more `tenant_id`s. Memberships are
`tenant_admin` or `operator`. API keys stay single-tenant. C5 console chrome
(Phase-4 gate) is not shipped; `GET /v1/me` and `POST /v1/auth/switch-tenant` exist.

## 5. Out-of-band integrations (preferred over reimplementation)

| Concern | Prefer | Console does |
| --- | --- | --- |
| Skill diff review | GitHub/GitLab PR | Show PR status + deep link; optional mirror in inbox |
| Metrics depth | Grafana / OTel | Summary cards + “open in Grafana” |
| Job schedules | cron / Actions / external scheduler | Manual trigger + last-run status |
| Secrets | Existing key CLI + provider env | Display redacted identity only |

## 6. Non-goals

- Embedding a full IDE or terminal emulator in the browser
- Fine-tuning or weight training UI
- Cross-tenant federated learning
- Replacing the golden gate with “approve in UI without eval”
- Claiming `a1`/`a2` supported from console charts alone (B7)

## 7. Relationship to the one-year roadmap

| Roadmap phase | Console relevance |
| --- | --- |
| Phase 1 operator GA | C0–C1 console accelerates Goal authoring and soak visibility |
| Phase 2 measured compounding | Ops panels consume lift/canary; do not invent new metrics |
| Phase 3 library economics | Tower panels surface ReplayPacks, retirement, correction proposals |
| Phase 4 tenant gate | C5 multi-tenant chrome only after readiness criteria |

## 8. Package layout (target)

```text
src/recertia/
  api/                 # existing FastAPI; grows console routes
  api/console_auth.py  # OIDC session helpers (C3)
  workers/             # async run worker (C2)
  proposals/           # durable proposal store (C1)
console/               # frontend package (C0+) — SPA or SSR
  # framework choice is an implementation detail; see implementation plan
```

The frontend MAY live in-repo under `console/` or in a sibling package; contracts and `/v1`
schemas remain the integration boundary (ADR-0009).
