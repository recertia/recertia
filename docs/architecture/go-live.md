# Single-user go-live

How to run Recertia as a local agent with a real model, allowlisted network tools,
and a lint-clean seed library.

## Credentials

| Provider | Env vars |
| --- | --- |
| Anthropic | `ANTHROPIC_API_KEY`, `RECERTIA_MODEL_PROVIDER=anthropic`, `RECERTIA_MODEL_ID=claude-…` |
| OpenAI | `OPENAI_API_KEY`, `RECERTIA_MODEL_PROVIDER=openai`, `RECERTIA_MODEL_ID=gpt-…` |
| Override key env | `RECERTIA_API_KEY_ENV=MY_KEY_VAR` |
| Optional verifier | `RECERTIA_VERIFIER_MODEL_ID=…` (same provider family) |

CLI shorthand:

```bash
recertia run --goal goal.json --model anthropic:claude-sonnet-4-20250514 --local-exec
recertia run --spec task.json --model openai:gpt-4.1
```

### OpenAI-compatible gateways (OpenRouter, etc.)

OpenRouter is configured as the OpenAI provider plus a gateway URL — not a separate
provider enum. Specs and remaining polish milestones:
[`openai-compat-gateways.md`](openai-compat-gateways.md),
[`../specifications/openai-compat-gateways.md`](../specifications/openai-compat-gateways.md),
[ADR-0013](../adr/0013-openai-compat-gateways.md).

Point the OpenAI client at a full Chat Completions URL and pass gateway metadata via env:

```bash
export RECERTIA_MODEL_PROVIDER=openai
export RECERTIA_MODEL_ID=moonshotai/kimi-k2   # exact OpenRouter slug
export OPENAI_API_KEY=sk-or-…
export RECERTIA_OPENAI_BASE_URL=https://openrouter.ai/api/v1/chat/completions
# Optional OpenRouter rankings / app attribution:
export RECERTIA_OPENAI_HTTP_REFERER=https://github.com/recertia/recertia
export RECERTIA_OPENAI_TITLE=Recertia
# Optional extra Chat Completions fields (JSON object; cannot override model/messages):
export RECERTIA_OPENAI_EXTRA_BODY='{"temperature":0.2}'
# Optional arbitrary headers (JSON object of strings):
# export RECERTIA_OPENAI_EXTRA_HEADERS='{"X-Custom":"value"}'
```

Stub (default) leaves the solver model unset so scratch fails loudly instead of
running a silent `true`. For offline demos only:

```bash
RECERTIA_ALLOW_STUB_MODEL=1 recertia run --spec task.json --model stub
```

## Verifier split

Prefer a distinct verifier model (and ideally a distinct credential) so the solver
cannot judge its own artifact:

```bash
export RECERTIA_VERIFIER_MODEL_ID=claude-haiku-4-20250514
# or: recertia run … --model anthropic:claude-sonnet-4-… --verifier anthropic:claude-haiku-4-…
```

## Cost accounting

Provider clients estimate `cost_usd` from token usage against a per-model pricing
table (`src/recertia/solver/pricing.py`). Override with
`RECERTIA_MODEL_PRICE_<PROVIDER>_<MODEL>_IN` / `_OUT` (USD per 1M tokens) or the
blanket `RECERTIA_DEFAULT_INPUT_USD_PER_MTOK` / `RECERTIA_DEFAULT_OUTPUT_USD_PER_MTOK`.

## Tools required by seed skills

| Tool | Role |
| --- | --- |
| `shell` / `edit_file` / `read_file` / `grep` | Repo chore primitives |
| `fetch` | Allowlisted HTTP GET (default hosts: `pypi.org`, `files.pythonhosted.org`, `raw.githubusercontent.com`, `api.github.com`) |
| `agent_subtask` | Model-backed one-command repair loop (needs a configured model) |

Tune fetch with `RECERTIA_FETCH_ALLOWLIST`, `RECERTIA_FETCH_TIMEOUT_S`, `RECERTIA_FETCH_MAX_BYTES`.

Fetched / tool text is delimited as untrusted data before it enters model prompts.
Model-proposed shell commands pass a prefix allowlist (`RECERTIA_COMMAND_ALLOWLIST`,
default repo-chore set). Break-glass only: `RECERTIA_COMMAND_POLICY=off`.

Scratch solving uses a bounded observe–act loop (`RECERTIA_SCRATCH_MAX_STEPS`,
default 5): each command’s output is fed back to the model within the attempt.

## Seed library

```bash
python3 scripts/install_seed_library.py --rewrite-versions
recertia skills lint
```

Approved seeds must carry hash-bound sensitivity proofs (`evidence_hash`). CI runs
`recertia skills lint` on every change that touches `skills/`.

## Promotion audit store

Promotion needs an operator-only audit store (REC-009). Set `RECERTIA_OPERATOR_AUDIT_ROOT`
(or pass `recertia skills promote --audit-root DIR`, or
`scripts/install_seed_library.py --promote --audit-root DIR`) to a private directory outside
any git checkout, the API root, every tenant runs root, the promotion log dir and the skills
root. Without a usable store the promotion is refused before the golden gate runs (API 503,
CLI exit 1).

The store records every failing fixture id, golden path and detail for a refusal; callers see
only the decision, the refusal kind, a failing-fixture count and an opaque `audit_ref`. An
operator prints a refusal's record with `recertia skills promote … --operator-detail`. The root
is set to `0700` and the database is created `0600`; a root owned by another user is refused.
File modes do not separate seats that share one OS user, so on a shared box treat the store as
readable by every seat and never record real held-out detail there.

`recertia eval run --golden-root/--golden-dir` refuses any path outside a git checkout. A
decrypted held-out split runs only through `recertia.evals.golden.run_heldout_suite`, with
its runs root outside any checkout.

## Console auth

Default is **off** (API keys only). Browser sessions are not issued unless you
opt in.

| Mode | Env |
| --- | --- |
| Off (default) | unset / `RECERTIA_CONSOLE_AUTH=off` |
| Dev login | `RECERTIA_CONSOLE_AUTH=dev` **and** `RECERTIA_CONSOLE_DEV_LOGIN=1`. Admin roles also need `RECERTIA_CONSOLE_DEV_ADMIN=1`. |
| OIDC | `RECERTIA_CONSOLE_AUTH=oidc` plus issuer/client id/secret **and** `RECERTIA_CONSOLE_SESSION_SECRET` (≥32 chars). PKCE S256 + one-time `state`. |

Cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` except in `dev`. The session
token is not returned in JSON and is not stored in `localStorage`.

Pending OIDC login starts are unauthenticated, so they are capped in process: 16 per
client, and 1024 in total as a backstop. The per-client check runs first: a client at its
limit loses only its own oldest start (it is not rejected, and no other client's entry is
touched), so one client's flood cannot evict or block another client's login. Only a start
from a client under its limit can meet a full table; it evicts the globally oldest pending
start and is accepted.

The client is the socket peer address. `X-Forwarded-For` is used for the client key only
when the peer is inside `RECERTIA_TRUSTED_PROXIES` (comma-separated CIDRs, empty by
default); it is otherwise inspected only to detect a proxy-headers rewrite (below). For a
trusted peer, every `X-Forwarded-For` line is joined in order and, walking from the right,
the client is the first hop that is not itself a trusted proxy. A missing header, a chain
made only of trusted proxies, or a malformed hop (anything but a bare IP, such as an empty
element or `ip:port`) met on that walk before the client falls back to the peer; malformed
hops left of the client are not looked at. `Forwarded` is never read, even from a trusted
peer. IPv4-mapped IPv6 addresses
(`::ffff:a.b.c.d`) count as the IPv4 address, and IPv6 clients are keyed by their /64. A
Unix-socket listener (`--uds`) has no peer address, so every login start through it shares
the fixed key `unknown` (one 16-slot limit). The per-client limit must stay below the global
cap; the module refuses to load otherwise.

**`RECERTIA_TRUSTED_PROXIES` is required for any proxied deployment.** Without it the app
sees only the proxy's address, so every user behind the proxy shares one client key and
one 16-slot limit: one user can then keep everyone else's logins failing. List only your
proxies' own addresses; `0.0.0.0/0` and `::/0` are refused at startup. The proxy must
append the address it received the connection from to `X-Forwarded-For` (for example,
nginx `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`), and must not pass a
client's header through unchanged as the only value. When `X-Forwarded-For` arrives from a
peer outside `RECERTIA_TRUSTED_PROXIES`, it does not change the key and a warning naming
`RECERTIA_TRUSTED_PROXIES` is logged once per process.

**Always start uvicorn with `--no-proxy-headers`** (as in the launch command below), or with
an empty `FORWARDED_ALLOW_IPS`.
uvicorn's default `--proxy-headers` trusts `X-Forwarded-For` from `FORWARDED_ALLOW_IPS`
(loopback by default; `--forwarded-allow-ips '*'` trusts everyone) and replaces the peer
address before the app runs. The app detects that rewrite: uvicorn leaves the header in
place, so a peer that equals one of its hops, or a peer that is not an IP address at all,
means something rewrote it. Such starts fail closed: they all share the fixed key
`proxy-headers-rewritten` (one 16-slot limit), so forged hops cannot mint new keys or evict
another client's login, and a warning naming `--no-proxy-headers` is logged once per
process. A trusted proxy that appends its own address is not a rewrite: when the peer is
trusted and a different hop remains to their left, its trailing self-hops are skipped
before detection. That skip relies on the rewriter setting the peer port to 0, which
uvicorn does for a bare hop (a real TCP peer never has port 0), so a rewritten peer never
gets it. `X-Forwarded-For` parsing is bounded before the header is split (16 header lines,
8192 bytes, 64 hops); anything larger gets the same fail-closed key, with its own one-time
warning.

**Warning: with uvicorn's default `--proxy-headers` behind a reverse proxy on loopback,
every user lands in one shared 16-slot bucket.** One client can then keep everyone's
logins failing. This is the fail-closed outcome, not a supported mode: fix the launch flags
(`--no-proxy-headers` plus `RECERTIA_TRUSTED_PROXIES`). A client that puts its own address in
`X-Forwarded-For` also lands in that shared bucket; it can only evict starts inside it.
When the bucket first reaches its 16-start limit, a second warning is logged once per
process. **Symptom:** all logins fail intermittently behind a proxy → check the logs for
`proxy-headers-rewritten`.

Telemetry keeps only the newest 1024 spans and 1024 events in memory, process-wide across
tenants; exporters still receive every record. In-memory readers (the telemetry dashboard,
its `missing_required` list, and `ops` snapshots without a spans file) see only that
window, so on a busy process an older required event can drop out and be reported missing.
Use an exporter or a spans file for a complete record.

Promote and improvement jobs require the `promote` / `jobs` API-key scopes (or
`admin`), or a console reviewer session. A `runs` key cannot approve skills.

## Jobs and retention

Policy: [`policy/default.json`](../../policy/default.json). Override with
`RECERTIA_POLICY_PATH`. Weekly spend is `{runs-root}/jobs/job_quota.json` (T0 sidecar).

```bash
recertia jobs run curator --dry-run
recertia jobs run practice                 # eligible fail-clusters first
recertia jobs run practice --one-off "lockfile drift"
recertia jobs run recertify                # stale certs + revoke drain
recertia jobs run mine --hint "docs/runbook.md" --submit
recertia gc --older-than-days 14 --dry-run
recertia gc --older-than-days 14
```

Jobs emit proposals / candidates only — never write `approved` (M7). HEX and compress stay
off until `practice_conversion` and a lift interval exist.

## Execution backend

Prefer containers for anything beyond local demos:

```bash
export RECERTIA_EXECUTION_BACKEND=container
# or: recertia run … --local-exec   # sets backend=local for this process
```

See [container-sandbox.md](container-sandbox.md).

## Measurement pins

Every CLI/API run pins `RunManifest.model`, `model_version`, `index_snapshot_id`, and
`library_commit` (git HEAD, or `RECERTIA_LIBRARY_COMMIT`) at start so lift and cost
metrics stay attributable.

## Console (Pilot / Tower / Ops)

Prefer the product console for day-to-day operator chores once the API is up:

```bash
# From a configured environment with RECERTIA_* model credentials as above:
python -m uvicorn recertia.api.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
# Open http://127.0.0.1:8080/console
```

| Surface | Use |
| --- | --- |
| Pilot | Goal form, templates, sync/async submit, live event stream; workspace select |
| Runs / Skills | Browse transcripts, promote (golden-gated) |
| Tower | Proposals, jobs (`dry_run` default), practice / pressure panels |
| Metrics | `MetricReport` + canary (unavailable reasons preserved) |
| Auth | Dev login / OIDC session; tenant switcher; **register workspaces** (admin) |

Issue an API key with `runs` (+ `metrics` / `exec` as needed) for the sidebar. Browser
sessions (`RECERTIA_CONSOLE_AUTH=dev` or `oidc`) carry human roles; do not embed long-lived
keys in frontend source. Specs: [`../specifications/product-console.md`](../specifications/product-console.md).

### Registered workspaces (real repo bind)

Pilot cannot take raw absolute `workdir` paths. Register an allowlisted host root first
(API process must resolve Windows drive-letter paths — run uvicorn on Windows for
`D:\…` roots):

```powershell
# Admin key (or console role admin + API key with runs)
recertia keys issue --tenant default --scopes runs,admin,metrics --actor dev

recertia workspaces register `
  --id recertia `
  --name "example/recertia" `
  --host-root D:\src\recertia `
  --tenant default `
  --runs-root .recertia

# CLI sugar
recertia run --goal evals/golden/repo-chore/add-editorconfig/goal.json `
  --workspace-id recertia --local-exec --runs-root .recertia
```

In the console: **Auth / Tenant → Register workspace**, then Pilot → Run → Workspace
select → Submit. Spec:
[`../specifications/registered-workspaces.md`](../specifications/registered-workspaces.md).

## Soak and durability (operator GA)

Durability unit: the entire `.recertia/` tree (checkpoints, operations, ledger,
snapshots, transcripts, episodic, skill index, API keys).

| Item | Guidance |
| --- | --- |
| Backup / RPO | Nightly `python3 scripts/backup_recertia.py` or `recertia backup`; target RPO ≤ 24h for single-operator |
| Postgres soak | `docker compose -f docker-compose.soak.yml up -d` then `DATABASE_URL=postgresql://recertia:recertia@localhost:5432/recertia python3 scripts/soak_postgres.py --recertia-root .recertia` (weekly via `.github/workflows/weekly-ops.yml`) |
| Dashboards | `GET /v1/metrics/dashboard` (scope `metrics`) or `recertia metrics`; OTel JSONL under the runs root |
| Retention | `recertia gc --older-than-days 14` on a weekly cron |
| SLOs (operator) | Run p95 latency and weekly eval-cadence tracked by the operator; alert on canary miss (`evals/canary/planted-failure`) |
| Quotas | `RECERTIA_TENANT_MAX_RUNS_PER_DAY`, `RECERTIA_TENANT_MAX_COST_USD_PER_DAY`, `RECERTIA_TENANT_MAX_IN_FLIGHT` |

Tabletop incident review: `recertia tabletop <run_id> --restore-from backups/….tar.gz`
writes the ops log JSON. See [`incident-tabletop.md`](incident-tabletop.md).
`recertia canary --live` scores planted failures with `RECERTIA_VERIFIER_MODEL_ID`
and does not update assumption `a4`.
