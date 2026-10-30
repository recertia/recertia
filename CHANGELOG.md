# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Migration programs carry an integer `version`. The store sets it to 1 on the first save
  and adds 1 on every later save, and it refuses a save that names a version other than the
  stored one.
- `PUT /v1/programs/{program_id}` replaces a draft program's editable fields. `If-Match` must
  carry the version last read; a stale version returns 409 `program_conflict` and nothing is
  saved. The compare and the write happen together in the store, so two writers that read
  the same version cannot both save.
- **Behaviour change:** for API clients, `PUT /v1/programs/{program_id}` on an existing
  program without `If-Match` returns 428 `precondition_required`.

### Changed

- `GET /v1/programs/{program_id}` saves the program only when refreshing step statuses
  changed something, so a read no longer moves `updated_at` or `version`.

## [0.4.0] - 2026-10-29

Pre-release, like 0.2.0 and 0.3.0. The package version and the API version (`info.version`
in the OpenAPI document) are now 0.4.0; through 0.3.0 both still said 0.1.0.

**0.2.0 and 0.3.0 are omitted.** Both were tagged and released without a section in this
file, and this release does not reconstruct them. The entries in this section cover every
change since 0.1.0, including changes that first shipped in 0.2.0 or 0.3.0, and are not
split by release.

### Upgrade notes

- **Binding a run to a program step needs a current preview (#260).** `POST
  /v1/programs/{program_id}/steps/{step_id}/run` with `bind_run_id` now returns `400` when the
  step has no preview, and `409` when the step's goal changed since its preview. Before, both
  binds returned `200`. Clients that preview the step, or ask for the run envelope, before
  they bind are unaffected. To fix a rejected bind, preview the step again, then bind.
- The other breaking changes in this release are listed under **Changed (breaking)** below:
  `GET /v1/runs` order and cursor (#136), SSE `id:` as a byte offset in follow mode (#137),
  the promotion API, CLI and library (REC-009), the negative-fixture API (REC-046), and the
  removed stub lock-identity helper (REC-056b1).
- **Supported versions:** `SECURITY.md` now names 0.4.x as the supported line. Older release
  lines (0.1.x, 0.2.x and 0.3.x) get no fixes; upgrade to 0.4.0 to receive them.

### Known limits

- **0.4.0 wheels leave out three things the package reads from outside itself.** Use a
  source checkout for these:
  - `console/static`, the web console. From a wheel install the `/console` routes return
    `404`.
  - `policy/authoring-prior.json`, the default authoring prior. From a wheel install a run
    that reaches the distill step ends with `authoring_prior_missing`.
  - The data that `src/recertia/evals/heldout.py` reads. That module is package code, but it
    assumes a source checkout: it looks for `evals/` in the working directory and at the
    repository root.
- **Low risk: the API probes a directory outside the package.** At startup the API checks
  with `is_dir()` for `console/static` at a path worked out from the installed package
  (`src/recertia/api/routes/meta.py:28`). For a wheel install that path is outside
  `site-packages`, and a wheel never creates it. If files were planted there, the API would
  serve them under `/console` without authentication. Anyone who can write there can
  already change the installed code.
- **Plan after 0.4.0:** move the web console, the default authoring prior and the held-out
  eval data under `src/recertia` as package data. The wheel then ships them, and the tests
  that check the wheel's `RECORD` and the sdist cover them. No task id is assigned yet.
- **`limit` is not checked at the request boundary on some list and search routes.**
  `GET /v1/jobs`, `GET /v1/proposals` and `GET /v1/reviews` clamp an out-of-range `limit`
  instead of rejecting it with `422`, and `POST /v1/skills/search` does not range-check
  `limit`; a non-integer `limit` there returns `500`. `GET /v1/runs` and the programs list
  already return `422`.
- **Some error-body tests check fields, not the whole body.** The tests for the `root_busy`
  responses on `/v1/runs` and `/v1/programs` assert the code, status and message, but not the
  exact error body, so a change that added a field (for example a server path) to that body
  would not fail them. The responses themselves carry no path.
- **The standalone worker does not end a failed run with a run event.** When a run fails
  inside `recertia worker`, the worker marks the queue item failed, but it writes no terminal
  event and no `run_failed` event for the run. The failure is visible in the queue status
  and in the worker's own output.

### Corrections

Earlier entries in this section are kept as written. Two known limits in the REC-058
root-lock entry under **Fixed** no longer hold:

- "`RootBusy` is not yet the `root_busy` API/CLI error": `RootBusy` now maps to the
  `root_busy` error (503, retryable) (#212, #248, #254).
- "The operations-schema exclusive lock is unbounded": the lock now waits with a bound
  (#212, #218). The other half of that item, about the faithfulness command's orchestrator,
  is not changed by this note.

### Pull requests since 0.3.0

Every pull request merged into `main` since the `0.3.0` tag whose title names a REC task id.
The entries below describe them where they are user-visible.

- #98 REC-020: commit-email check covers the pushed range (before..after) on push
- #100 REC-011 (C1, part 1): record run resolution (decided|unresolved|invalid) + exit_code + reason
- #101 REC-013: T2 planted-regression set (10 cases) + MANIFEST.sha256
- #102 REC-022: operator-only held-out eval job (fail-closed, container-only) [draft]
- #103 REC-022: weekly-ops least privilege, public eval roots only, no ${{ }} in any run: [draft]
- #104 REC-024: redact non-allowlisted values in commit-email check output
- #105 REC-025: check every address in a \*-by trailer value
- #106 REC-026: stop the embedded queue worker before the manual cap claim (test_resume_admits_quota flake)
- #107 REC-028: m2 concurrency test asserts overlap/ordering, not wall clock
- #108 REC-029: safe embedded worker start/stop, stop() reports timeout, no leaked test threads
- #109 REC-028 nits: clear failure on lost concurrency, 5 s rendezvous, second rendezvous
- #110 REC-027: harden commit-email check
- #111 REC-030: spawn ledger appenders with deadline (defence in depth)
- #112 REC-017: bounded probe harness, spawn context, pytest-timeout backstop, CI job timeouts
- #113 REC-031: bounded worker teardown (F1–F4)
- #114 REC-035: mypy ratchet covers tests/
- #115 REC-036: session exit guard, CI job timeouts, trylast test (stacked on #113)
- #116 REC-040: mypy ratchet follow-ups (S4, S5, N2, N4-N7) from PR #114 review
- #117 REC-042: restore the network guard for the outermost session only (stacked on #115)
- #118 REC-009 Gap A: decision-only promotion refusals, operator audit store, held-out manifest loader
- #119 REC-044: fix-forward for REL-3 probe hang path (D1-D3, N1)
- #120 REC-017: faulthandler watchdog + spawn for ledger concurrency (recut of #96)
- #121 REC-046: stop leaking recertia-neg-\* temp dirs from sensitivity proofs
- #122 REC-048: pytest-timeout floor 2.4.0 (nightly lowest-deps ResolutionImpossible) [draft]
- #124 REC-047: rel3 probes clean up their temp dirs
- #127 REC-049: raise the pytest floor to 9.0 (faulthandler_exit_on_timeout) [r2, supersedes #126]
- #128 REC-050: exit-code test uses public typer.Exit [r2, supersedes #125]
- #129 REC-053: declare click>=8.2 in the dev extra; exit-code test asserts stderr, not stdout
- #141 REC-051: nightly runs the full suite on both dependency arms (v2, supersedes #123) [draft]
- #149 REC-052: harden probe temp-dir cleanup and close probe pipes/files (r2, restack on e25ecfa6)
- #151 [REC-056b1] REL-3 R4: delete stubbed lock-identity helper, update probe T3 (#132) (r2, restack on 87bbc1ae)
- #161 REC-056a: cap telemetry deques and pending OIDC logins per client (#131)
- #164 [REC-056b2] Un-skip REL-3 R4 probes I, L, M, M2, T1, T2 as typed RootBusy per ADR-0022 (#139)
- #167 REC-056a: follow-up to #161 (untrusted-XFF warning wording, XFF cap-order docs) [r2, restack on 17943bf3]
- #168 REC-058 r3: root lock for API handles, retryable RootBusy, off-mode re-check, real-Windows lock (carries #166)
- #169 REC-058 follow-up to #168: typed RootBusy for the Windows off-mode wait; pin mid-run RootBusy and the 2 s back-off
- #170 [REC-065] Per-tenant root locking so tenants on one root do not exclude each other
- #176 [REC-069] REC-058 follow-up to #169 (restack of #171 onto 33a88a40, r4): pin the fd close and non-busy mapping on the blocking path; api.lock CHANGELOG
- #177 [REC-071] #170 review follow-ups: one-deadline test, backup lock-file match, tenant export skip, docstrings, ADR-0022 wording
- #178 [REC-070] manual mode names the real import procedure instead of the missing migrate apply
- #179 REC-057: raise psycopg floor to >=3.2.1
- #180 nightly: add extras floors, pin script, persist-credentials, and alert job (REC-057/062 remaining)
- #182 REC-064: overwrite with bounded wait on migrate=off path
- #186 [REC-073] real-Windows migrate=off tests expect the bounded wait added in #182
- #187 [REC-070] manual-mode advice: reopen with auto only once no older-release process uses the root
- #188 [REC-075] Pin the migrate=off bound with a POSIX test; steady real-Windows bounds; fix stale CHANGELOG
- #189 [REC-076] Restore the nightly CHANGELOG entries; drop an agent name from a workflow comment
- #190 [REC-077] Tighten two root-lock test asserts; harden the manual-mode docs tests
- #191 [REC-072] Follow-up to #177: pin every runs-root .tenant.lock position, make tenant export --dry-run match --execute, fix the lock wording
- #192 [REC-078] Remove agent and person names from workflow comments
- #193 [REC-079] Remove old review labels, pin the lock poll interval, fix changelog wording
- #195 [REC-081] Drop leftover review tags and work-order ids from comments and docstrings
- #196 [REC-060] Include ADR-0020 to ADR-0022 in docs/architecture2.md
- #197 [REC-082] Drop remaining internal review tags from comments and docstrings
- #198 [REC-083] Fix a test header and drop remaining internal review labels
- #199 [REC-084] Rename review-tagged test names
- #200 [REC-085] Rename single-letter-tag test names
- #201 [REC-086] Replace ruling-phrase wording
- #202 [REC-087] Remove internal wording from comments, docs and test labels
- #203 [REC-088] Remove leftover bug-number wording from comments
- #205 [REC-089] Remove reviewer provenance and finding ids from ADR-0022
- #213 REC-063/REC-066: T3 history check; root-lock or documented exception for direct stores
- #214 [REC-059] Add RootBusy class, exit-code and retryable-flag tests
- #218 [REC-067] Narrow the ops-schema lock exception mapping, reuse the lock-timeout helper, drop the path from the busy message
- #220 [REC-059] Round Retry-After up to whole seconds in recertia_error_response
- #223 [REC-059] Make root_busy retryable on envelopes built by the error helpers and init RootBusy via RecertiaError
- #226 [REC-067] Test the operations-schema lock: own shared lock self-deadlock case and success after release
- #232 [REC-059] Add optional retry_after_s to v1_error and validate Retry-After in one helper
- #234 [REC-056g] SSE follow mode with byte-offset ids and Last-Event-ID resume
- #239 [REC-056f] Add an opaque keyset cursor for GET /v1/runs ordered by created_at desc, run_id desc
- #241 [REC-056g] SSE follow: lone-error doc fix, gap-kill tests, ASGI idle-slot caveat
- #244 [REC-066] Tidy duplicated docstrings, exception reasons and a dead fallback
- #248 [REC-059] Return root_busy (503, retryable) from GET, create, resume and cancel under /v1/runs when the root is busy
- #250 [REC-067] Keep lock and root paths out of RootBusy messages
- #251 [REC-056g] SSE follow: end the stream for a run the queue marks dead
- #254 [REC-059] Map RootBusy to root_busy on the /v1/programs routes
- #256 [REC-067] End an async run when opening its persistence fails with a non-busy OSError
- #259 [REC-056g] SSE follow: end the stream for a run the queue marks failed
- #260 [REC-059] Check the step's preview before binding a run

#### Other changes

Pull requests merged in the same range without a REC id in the title: #54-#71, #73, #74,
#76, #78-#80, #82-#90, #92-#94, #210, #212.

### Security

- **In-process buffer caps (REC-056a, #131)** — `Telemetry.spans` and `Telemetry.events` are
  now bounded deques (`maxlen=1024`) that drop the oldest entry first. The pending OIDC
  login map is capped at 16 starts per client, and at 1024 in total as a backstop.
  `SessionStore.begin_oidc` takes a new `client_key` argument. The per-client check runs
  first: past its limit a client loses only its own oldest start, so one client's flood
  cannot evict or block another client's pending login. A start from a client under its
  limit that meets a full table evicts the globally oldest start and is accepted. The
  client is the socket peer. The new `RECERTIA_TRUSTED_PROXIES` setting (`trusted_proxies`,
  comma-separated CIDRs, empty by default) lets the login route use `X-Forwarded-For` for
  the client key only from a peer inside those networks, taking the rightmost hop that is
  not a trusted proxy; otherwise the header is inspected only to detect a rewrite.
  A missing header, a malformed hop or an all-proxy chain falls back to the peer, and
  `Forwarded` is never read. The per-client limit must stay below the global cap (checked
  at import). All `X-Forwarded-For` lines are joined in order; IPv4-mapped addresses fold
  to IPv4 and IPv6 clients are keyed by /64. `0.0.0.0/0` and `::/0` are refused. A peer
  rewritten by uvicorn's proxy-headers layer (the peer equals an `X-Forwarded-For` hop, or
  is not an IP) is detected and fails closed to one fixed shared key with a one-time
  warning, and another when that key first fills; with uvicorn's default behind a
  loopback proxy that puts every user in one 16-slot bucket. A trusted proxy's own
  trailing self-hops are skipped before detection; that relies on the rewriter setting
  port 0 (true for uvicorn). `X-Forwarded-For` parsing is bounded: 16 lines and 8192
  bytes are checked before splitting, 64 hops right after (larger fails closed). A
  Unix-socket peer uses the fixed key
  `unknown`. `X-Forwarded-For` from an untrusted peer logs a one-time warning naming
  `RECERTIA_TRUSTED_PROXIES`.
  **Operators: `RECERTIA_TRUSTED_PROXIES` is required behind a reverse proxy** (otherwise
  every user shares one 16-slot key), the proxy must append to `X-Forwarded-For`, and
  uvicorn should run with `--no-proxy-headers` (the documented launch commands now pass
  it). In-memory telemetry readers (dashboard, `missing_required`) see only the newest
  1024 events, process-wide.
- **Fixture-id leak (REC-009)** — a golden-gate refusal is now decision-only for
  proposers. `PromotionError` carries `refusal_kind` (`golden_gate` |
  `predecessor_non_regression` | `gate_error` | `audit_unavailable`), `failing_count` and an
  opaque `audit_ref`. Fixture ids, golden paths, result rows and raw error text go to an
  operator-only SQLite store under `RECERTIA_OPERATOR_AUDIT_ROOT`
  (`recertia.review.promotion_audit`), which fails closed: promotion is refused before the
  golden gate runs when the store is unset, sits inside a git checkout, the API root or a
  tenant `runs_root`, or cannot be opened, and a write failure after the gate also refuses.
  The root is set to `0700`, the database is created `0600`, and a root owned by another user
  is refused. The promote HTTP body, the promote job record (`GET /v1/jobs`,
  `GET /v1/jobs/{id}`; older records are redacted on read), the refusal promotion log and CLI
  stderr no longer name fixtures, paths or exception text; `recertia skills promote
  --operator-detail` prints the audit record. Held-out splits get a strict hash-manifest
  loader (`recertia.evals.heldout_manifest.verify_heldout_tree`) and a schema under
  `evals/heldout/`, with no data committed. `recertia.evals.golden.run_heldout_suite`
  verifies the split first, refuses a runs root inside a git checkout or overlapping the
  split, takes each fixture's skill from `task.json` `expected_skill_id` (never from a dir
  name such as `h001`), masks `golden_path` and the skill-bearing run id at the source, and
  removes each fixture's run state (runtime db, trajectories, copied workspace) afterwards.
  `recertia eval run` refuses a `--golden-root`/`--golden-dir` outside a git checkout.

### Changed (breaking)

- **SSE `id:` is a byte offset in follow mode (#137)** — `GET /v1/runs/{run_id}/events` takes
  a new `follow` query flag. With `follow=1` the stream stays open and sends events as they
  are appended. **Contract break:** each frame's SSE `id:` is now the byte offset just past
  that event in the run's log, no longer the event id, so a client that stores ids and sends
  them back as `Last-Event-ID` must store the offset. Without `follow` the stream and its
  event-id `id:` are unchanged. `Last-Event-ID` is the cursor (a plain decimal offset that is
  `0` or a frame's `id:`); `?after=<event_id>` is translated once to an offset and is
  overridden by the header. A bad cursor (sign, spaces, underscore, empty or repeated header,
  past the end of the log, inside a line, unknown `after`) is a `400` before streaming starts.
  Every frame has its own `id:`, a half-written line is delivered once complete, and the
  stream ends after `run.finished` or `run.cancelled` (by event type), including for a run
  not held in memory, or after `RECERTIA_SSE_MAX_S` (default 30 s). Open follows are capped
  per tenant (`RECERTIA_TENANT_MAX_SSE`, default 16) and in total
  (`RECERTIA_GLOBAL_MAX_SSE`, default 64) with `429` and `Retry-After`; a waiting follow does
  not hold a worker thread, so `/ready` and the other endpoints stay responsive. New
  settings: `RECERTIA_SSE_POLL_S` (0.5), `RECERTIA_SSE_KEEPALIVE_S` (15). An `event:` name
  that is not a single line of printable text is sent as `unknown`, in both modes. See
  `docs/specifications/product-console.md` section 4.3.

- **Stub lock-identity helper removed (REC-056b1, #132)** —
  `recertia.persistence.root_lock.exclusive_held_by_this_thread` is deleted. It was a stub
  that always returned `False`: the root lock is the flock alone and the `.lock` file text is
  never holder identity, so there is nothing to replace it with. Importing it now raises
  `ImportError` (`tests/unit/persistence/test_import_legacy.py`). The REL-3 R4 probe T3
  (`tests/probes/rel3_probe_r4.py`) no longer calls it and checks only that an open with
  leftover pid/thread text in `.lock` and no flock held still imports the leftover files. The
  six skipped R4 probes stay skipped (#139).

- **Negative-fixture API (REC-046)** — `recertia.validation.sensitivity.empty_negative_fixture`
  is removed; call `author_sensitivity_proof(criterion)` without `negative_workdir` (it owns
  and removes the directory), or pass a directory you own. The critic's negative fixture
  is now always created in the system temp dir (`tempfile.gettempdir()`, which honours
  `TMPDIR`), never inside the task workdir, so that directory must be writable. For that
  reason `propose_criteria` and `refine_goal_criteria` (`recertia.validation.critic`) no
  longer take a `workdir` keyword: it only chose where the fixture was created.

- **Promotion API, CLI and library (REC-009)** —
  - `PromotionError.failing_fixtures` is removed; use `refusal_kind`, `failing_count`
    and `audit_ref`. `promote_to_approved` requires `audit=` (an operator audit sink);
    `None` refuses with `refusal_kind="audit_unavailable"`. Gate `ValueError`/`OSError`
    now raise `PromotionError(refusal_kind="gate_error")` with a generic message.
  - `POST /v1/skills/{id}/versions/{n}/promote`: the `failing_fixtures` response field is
    removed and `decision`, `refusal_kind`, `failing_count` and `audit_ref` are added; the
    refusal `error` text changed. An unavailable audit store is 503 (before the gate), an
    unknown skill or version is 404 `skill version not found`, and an unexpected error is
    400 `promotion failed: internal error; operator detail: audit_ref=…` instead of the raw
    exception text. Promote job `meta` gains the same decision fields.
  - `recertia skills promote` needs `--audit-root` or `RECERTIA_OPERATOR_AUDIT_ROOT` (exit 1
    before the gate otherwise), drops the `failing_fixtures=` stderr line and gains
    `--operator-detail`. `scripts/install_seed_library.py --promote` needs `--audit-root`.
  - `recertia eval run` exits 2 for a `--golden-root`/`--golden-dir` outside a git checkout.

- **`GET /v1/runs` lists newest first with an opaque cursor (#136)** — the list was in
  run-id order and is now newest first (`created_at` descending, run id descending).
  `next_cursor` is an opaque keyset cursor instead of the last run id, and a walk with
  `limit` pages has no 10,000-run ceiling. A walk lists each run once while no run gets its
  first checkpoint during it. A run that does can be listed twice or skipped: its position
  moves from its registry date to its checkpoint date, and checkpoint dates are cut to whole
  seconds. A cursor that is malformed, over 512 characters or not in the canonical form
  returns `400` `invalid_cursor`, and so does a cursor from before this change (a bare run
  id): restart the walk without a cursor. Runs that were accepted but have no checkpoint yet
  are now listed (status `accepted`) and take their stored state once they have one; they
  match neither the `task_class` nor the `terminal` filter. `limit` stays 1 to 100, and a
  value outside that range is still `422`. A page reads about `limit` rows from each table,
  plus the runs whose registry date falls between the page's top and bottom but whose
  checkpoint date does not. Accepted-only runs between two checkpointed runs stay bounded at
  about `limit` rows (51 at `limit=50`). The costly case is a batch of runs accepted
  together whose first checkpoints land later: the page that covers the batch reads every
  run in it (about 100,000 rows and 0.32 s for a 100,000-run batch), while a whole walk
  stays linear in the number of runs. Opening the registry and the checkpoint store adds the
  `runs_list` index on `runs` and the `run_summaries_created` index on `run_summaries`, so
  the first request after an upgrade builds them.

### Fixed

- **The wheel ships every persistence migration, so the API starts from an installed wheel**
  — the package data covered only the store migrations. The control, runtime and quota
  migrations, and the notes kept beside the Postgres migrations, were left out of the wheel
  and the sdist, so starting the API on a fresh root from an installed wheel failed with
  `no such table: run_queue`. The wheel and the sdist now ship all of them. No released
  version is affected: those migrations arrived with #69, after the `0.3.0` tag, so 0.4.0
  would have been the first release without them. A new test builds the wheel offline,
  checks that its `RECORD` lists every non-Python file in the package, installs it into a
  temporary venv and starts the API there. Another builds the sdist offline and checks that
  it carries the same files. The `dev` extra now includes `setuptools`, which those offline
  builds need.
- **A missing authoring prior raises a named error with no absolute path** — wheels do not
  include the default authoring prior, and a successful run loads it when it drafts a skill.
  From a wheel install that was a bare file-not-found error carrying the absolute path it
  looked in, and `recertia run` printed a traceback. It is now `AuthoringPriorMissing` (code
  `authoring_prior_missing`), and its message names the missing prior and says to run from a
  source checkout. A prior path passed in by the caller is named by file name only.
  `recertia run` and `recertia resume` now print a Recertia error as `code: message` and exit
  with its code (7 for this one) instead of printing a traceback.
  **Behaviour change:** a run started through `POST /v1/runs` that ends on a Recertia error
  now has an `error` event with that error's own code instead of `run_failed`. Event and SSE
  consumers can now see `authoring_prior_missing`, `busy`, `conflict`,
  `execution_unavailable`, `forbidden`, `internal_error`, `invalid_config`,
  `invalid_cursor`, `invalid_input`, `lease_lost`, `ledger_unavailable`, `lock_timeout`,
  `not_found`, `quota_exceeded`, `rate_limited`, `receipt_error`, `run_id_exists`,
  `secret_unavailable`, `state_corrupt` and `unauthorized`. Other exceptions still give
  `run_failed`, and the run's queue record keeps `error_code` `run_failed`.
- **`recertia run --local-exec` rejects a malformed spec or goal with exit 2 and no
  traceback** — with `--local-exec`, a `--spec` that is not valid JSON or a `--goal` that
  fails validation (for example `{}`) printed a traceback with absolute paths and exited 1.
  It now exits 2 with one `invalid_config: ...` line that names only the file.
- **Binding a run to a program step checks the step's preview** — since 0.1.0, `POST
  /v1/programs/{program_id}/steps/{step_id}/run` with `bind_run_id` replaced the step's
  `criteria_preview_hash` with the hash of the goal it had just materialized before its two
  checks read it, so neither could fire: a bind with no preview, or after the goal changed
  since the preview (for example new freeze paths under `hard` enforcement), went through.
  Found in review of #254. The checks now read the hash the last preview stored, and answer
  as documented: `400` `preview step before bind (missing criteria_preview_hash)` and `409`
  `goal changed since preview; re-run preview before bind`. **Behaviour change:** a client
  that binds without first calling `/preview`, or `/run` without `bind_run_id`, now gets
  `400`, and one whose step goal changed since that call gets `400` or `409`. A bind no
  longer stores the hash before its checks, so a rejected bind leaves the program unchanged
  and does not count as a preview. A `plan_only` call or one without `bind_run_id` still
  stores it, as does a bind that succeeds. Clients that preview, or ask for the run
  envelope, before they bind (the console does) see no change.
- **A non-busy lock failure ends an async run instead of leaving it queued (#160)** — when
  opening a run's persistence failed with an `OSError` that is not a busy refusal (for
  example the lock path is a directory, or the disk is full), the in-process async worker
  let the error escape: the run's events stopped at `run.started`, no `run.finished` was
  written, and the run record stayed `queued`. The worker now writes an `error` event with
  code `run_failed`, `retryable: false` and a message that names only the exception class
  (`cannot open the run's persistence (IsADirectoryError)`), then `run.finished` with
  `terminal: "error"`, and calls the failure hook, so the run record ends as `error` instead
  of staying `queued`. The failure hook runs even if writing those events fails (for example
  on a full disk). The full error, path included, goes to the server log only. A busy refusal
  (`RootBusy`) is still retryable and requeued.
- **`/v1/programs` routes report `root_busy` while the runtime root is busy (#145)** — the
  routes under `/v1/programs` share one error mapper, and it turned a `RootBusy` from the
  program service into `503` with code `unavailable` and `retryable: false`. Binding a run
  to a step (`POST /v1/programs/{program_id}/steps/{step_id}/run` with `bind_run_id`) can
  hit this: when the run's record has no criteria hash it reads the run's checkpoints,
  which needs the runtime root lock. The mapper now leaves a `RootBusy` to the app-level
  handler, so every programs route sends the same envelope as `/v1/runs`: code
  `root_busy`, `503`, `retryable: true`, with no `Retry-After`.
- **SSE follow ends for a run the queue marks failed with no terminal event (#137)** — a run
  the worker ended with `run_busy`, `workspace_busy` or a start-up failure other than an
  `OSError` opening its persistence is marked `failed` in the queue but gets no terminal event
  in its log, so a `follow=1` stream held until `RECERTIA_SSE_MAX_S`. It now ends the way a
  dead run does: after one more read of the log it sends `run.finished` with payload
  `{"terminal": "error", "error_code": ...}` (the queue's code), and a real `run.finished` in
  the log still wins. The re-read of such a run now stops at `RECERTIA_SSE_MAX_S`, and a log
  that ends with a real terminal event and then a partly written line no longer gets a second,
  synthetic `run.finished`. Section 4.3 of `docs/specifications/product-console.md` now lists
  `401` among the JSON error responses and notes when the queue's answer can go stale. A run
  whose persistence fails to open with an `OSError` already gets an `error` event and a
  terminal `run.finished` (#160), so its follow ends on that event.
- **SSE follow ends for a run the queue marks dead (#137)** — a run whose worker process died
  on its last attempt is marked `dead` (`lease_expired`) and gets no terminal event in its
  log, so a `follow=1` stream held until `RECERTIA_SSE_MAX_S` and the client reconnected
  without end. Once a follow has sent the whole log of such a run, it now sends
  `run.finished` with payload `{"terminal": "error", "error_code": ...}` and ends. It reads
  the log once more first, so a `run.finished` the worker wrote just before it died is sent
  instead. The frame has no `id:` (it is not a line of the log), so a reconnect gets it again
  and ends. The shared SSE response headers are now read-only.
  `docs/specifications/product-console.md` section 4.3 documents the dead-run end and the
  `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers, and its ASGI 2.4 note now
  says that Starlette stops watching for the disconnect and that an idle slot can be held for
  up to `RECERTIA_SSE_KEEPALIVE_S` + `RECERTIA_SSE_POLL_S`.

- **`RootBusy` messages carry no filesystem path (#160)** — a refused open or leftover import
  put the lock path or the runtime root path in the `RootBusy` text, which reaches the
  `root_busy` run event and logs. `TenantLock.refused` is now `root lock is held`
  or `tenant lock is held`, and the two re-check messages in `open_run_persistence` no longer
  end in the root path. The wording otherwise stays: `cannot open the runtime root while the
  root lock is held`, `cannot import leftover runtime files while the tenant lock is held`,
  `leftover runtime files appeared while waiting for the root lock`, `the runtime root was
  imported while waiting for the root lock`. Anything that matched the path in these
  messages must match the lock name instead. `RootLockBusy` (the cause, not the `RootBusy`
  text) still names the lock file.
- **`GET /v1/runs/{run_id}` reports `root_busy` while the runtime root is busy (#145)** — when
  the runtime root lock is held (a backup, a restore, or another process importing leftover
  run files), `GET /v1/runs/{run_id}` answered `503` with code `unavailable` and
  `retryable: false`. It now sends the same envelope as the other routes: code `root_busy`,
  `503`, `retryable: true`, with no `Retry-After`. A client can retry the request once the
  lock is released. The fix is in the error mapper that create, fetch, resume and cancel
  under `/v1/runs` share: it turned a `RootBusy` raised by the run service into
  `unavailable` / `retryable: false`, and now gives `root_busy` on all four. A create while
  the lock is held is still accepted (`202`, queued), and cancel's run lookup, which runs
  before the mapper, already answered `root_busy`.

- **SSE follow: docs match `error` and disconnect handling; no proxy buffering (#137)** —
  `docs/specifications/product-console.md` section 4.3 now says that an `error` event never
  ends a follow stream by itself (a failed run ends with `run.finished`, `terminal: "error"`),
  that on an ASGI 2.4 server an idle follow's slot is released at the next keep-alive at the
  latest once the server notices the client has gone, and that the follow caps are counted
  per server process. Both `GET /v1/runs/{run_id}/events` responses (backlog and
  `follow=1`) now send `Cache-Control: no-cache` and `X-Accel-Buffering: no`, so a buffering
  proxy such as nginx passes frames on at once. New tests cover a carriage return in an event
  type, the 128-character event-name cap, the sleep between idle polls, a terminal event
  followed by a half-written line, a backlog larger than one bounded read, and a lone
  non-retryable `error` that holds the stream until `RECERTIA_SSE_MAX_S`.

- **`Retry-After` rounds up to whole seconds (#145)** — `recertia_error_response` truncated
  `retry_after_s` (`int()`), so a fractional value was sent short: `0.2` gave `Retry-After: 0`
  and `1.2` gave `1`. It now rounds up (`0.2` gives `1`, `1.2` gives `2`, `2.5` gives `3`).
  Whole values are unchanged, and no header is sent for a missing `retry_after_s`. This
  changes the value sent for any `RecertiaError` with a fractional `retry_after_s`.

- **`manual` mode named a command that does not exist (REC-070, #172)** — with leftover
  `checkpoints.db` / `operations.db` still pending, `RECERTIA_RUNTIME_DB_MIGRATE=manual`
  refused to open with "run recertia migrate apply" (message and `details.action`), but
  the CLI has no `migrate apply`; `recertia migrate` has only `run-events`. The error and
  its `details.action` now give the real procedure, condition first: only once no process
  of an older release uses this root, reopen with `RECERTIA_RUNTIME_DB_MIGRATE=auto` (the
  default); otherwise stay on `RECERTIA_RUNTIME_DB_MIGRATE=off`. An older `migrate=off`
  writer can lose its later writes to that import (see the upgrade requirement under
  REC-065 below). The open that gets the locks imports the pending files into
  `runtime.db`; on `root_busy`, retry. The error type (`MigrationChecksumError`,
  `state_corrupt`) is unchanged. Tests:
  `test_manual_mode_error_names_only_registered_cli_commands` in
  `tests/unit/persistence/test_import_legacy.py` pins the condition and its place before
  the advice, the retry hint, the `off` fallback, that the named settings are read, and
  that the text names no ticket id, commit hash or version tag;
  `test_manual_mode_advice_is_pinned_in_changelog_and_rel_specs` pins the same condition,
  the `auto` reopen and the `root_busy` retry in this file and in `rel-specs.md`.

- **`migrate=off` open with pending leftover files has a bounded wait (REC-064, #153)** —
  `open_run_persistence(..., migrate="off")` on a root that still has `checkpoints.db` /
  `operations.db` took the shared root lock with no bound: against a holder it waited
  forever on POSIX, and on Windows until the 10 s retry budget ran out, when it raised
  typed `RootBusy` (the chain was `OSError(EDEADLK)`, wrapped in `RootLockBusy`, wrapped
  in `RootBusy`). It now waits at most `RECERTIA_IMPORT_LOCK_TIMEOUT_S` (default 5 s),
  like the other opens, on every platform, and then raises typed `RootBusy`
  (`root_busy`, caused by `RootLockBusy`) without writing anything; a retry after the
  holder leaves opens the leftover files and sees their data. Tests:
  `test_migrate_off_leftover_open_wait_is_bounded`
  in `tests/unit/persistence/test_import_legacy.py` fails if the bound is removed, zeroed
  or ignores the setting; the real-Windows tests in
  `tests/unit/persistence/test_root_lock_windows_real.py` expect the bounded wait (a
  `BlockingIOError(EAGAIN)` cause, one lock-file open per try, all closed).

- **Per-tenant root locking: tenants on one root no longer exclude each other (REC-065,
  #158)** — each runs root gets its own lock file, `<runs_root>/.tenant.lock`
  (`RunsRootLayout.tenant_lock`), nested under the runtime root's `.lock`. Locks are taken
  in one fixed order, documented in `persistence/root_lock.py`: `api.lock` (backup and
  restore only), then the root `.lock`, then one tenant lock.
  - The leftover import takes the root `.lock` shared, then its tenant lock exclusive
    (before: the root `.lock` exclusive). A run handle (`open_run_persistence`) takes the
    root `.lock` shared, then its tenant lock shared, for the handle's lifetime. Both
    waits, and the `migrate=off` leftover path since REC-064 (#153), share the one
    `RECERTIA_IMPORT_LOCK_TIMEOUT_S` bound.
  - So an open handle or an import on one tenant, including a long worker run, no longer
    makes another tenant's pending import raise `RootBusy`, and imports of different
    tenants run at the same time. A second import of the same tenant, or an import while
    a handle of the same tenant is open, still waits within the bound and then raises
    typed `RootBusy` without writing; the message names the lock that was held.
  - Backup and restore are unchanged: they take the root `.lock` exclusive, which every
    tenant import and handle holds shared, so they still refuse while any tenant holds
    its lock.
  - Backup does not archive a runs root's `.tenant.lock` and restore does not extract it,
    as for `.lock` and `api.lock`. Only the four runs-root positions match: `.tenant.lock`
    at the runtime root (the CLI layout), `jobs/.tenant.lock`, `runs/<tenant>/.tenant.lock`
    and `runs/<tenant>/jobs/.tenant.lock` (restore matches after `\` becomes `/`). A file
    of that name anywhere else is user content: it is archived and restored.
  - `recertia tenant export` leaves the tenant's two runs-root lock files
    (`runs/<tenant>/.tenant.lock`, `runs/<tenant>/jobs/.tenant.lock`) out of both
    `--dry-run` and `--execute`, so the files the listing names are the files the copy writes.
    The listing also names directories; `--execute` copies files only.
  - **Upgrade requirement: mixed versions can silently lose data.** Before starting any
    REC-065 process on a runtime root, stop every REC-058-or-older process on that root
    (workers, API, CLI). Do not do a rolling restart, and do not mix versions.
    - Consequence: an older `migrate=off` handle on a tenant whose leftover files are not
      yet imported can have its later writes silently lost if a newer process imports
      that tenant meanwhile. A REC-058 handle takes only the root `.lock` shared, so it
      does not keep a newer import of the same tenant out; neither process reports an
      error.
    - If you cannot stop every process at once: keep the new processes on `migrate=off`
      until every old process is gone. A `manual` open of a tenant whose leftover files
      are pending refuses with `state_corrupt` and cannot serve that tenant, so `off` is
      the only choice there. Only once no process of an older release uses this root is
      it safe to reopen with `RECERTIA_RUNTIME_DB_MIGRATE=auto`; while an old process is
      still running, ignore the `manual` error's advice to reopen with `auto` and stay on
      `off`.

  Tests: `tests/unit/persistence/test_root_lock_writers.py` (REC-065 section),
  `tests/unit/persistence/test_root_lock_windows_path.py`,
  `tests/unit/persistence/test_root_lock_windows_real.py`. Three REC-058 tests that
  pinned the cross-tenant `RootBusy` now assert that the other tenant proceeds, or use
  an import of the same tenant as the holder.

- **Run handles honour backup/restore's root lock, including workers (REC-058, #144)** —
  correction to the REL-3 runtime.db entry below, which read as if every opener honoured
  `.lock`. Only the leftover-import path did. On a root whose import had already completed,
  `open_run_persistence` took no root lock, and workers open `<root>/runs/<tenant>`, whose
  `.lock` is not the `<root>/.lock` that backup and restore take. So CLI and worker handles
  could write while backup or restore held the exclusive lock (the API was kept out only by
  `api.lock`). Now:
  - Every handle from `open_run_persistence` holds a shared root `.lock` from open until
    `close()`, on fresh, imported and leftover roots. The queue worker (`recertia worker`)
    and the embedded async worker pass the new `runtime_root` argument
    (`open_run_persistence`, `build_default_orchestrator`), so their handles lock
    `<root>/.lock`. Files and tenant id are unchanged.
  - On a fresh or imported root, an open that finds the exclusive lock held waits at most
    `RECERTIA_IMPORT_LOCK_TIMEOUT_S` (default 5 s), then raises typed `RootBusy` before any
    run-store db file is opened. Leftover files are re-checked once the lock is held: an
    open that waited out a restore which brought them back also raises `RootBusy`, and the
    retry imports them. Before, such an open served its pre-restore decision and could
    lose a leftover checkpoint. `migrate=off` re-checks too: an open that chose the
    leftover files but finds the root imported once it holds the lock (a restore of a
    post-import backup) raises `RootBusy`; before, its writes went to files no later open
    reads, so they were lost.
  - The API process's own tenant opens (`CheckpointReader`, used by
    `GET /v1/runs/{id}/transcript`, and the run loader) pass `runtime_root` too, so API
    and worker leftover imports on one tenant exclude each other. Before, they locked
    different files; both could import at once and one could fail with an untyped
    `sqlite3.IntegrityError`.
  - `RootBusy` is retryable on the worker path. `recertia worker` and the API's embedded
    queue worker return a refused claim to `queued` after a 2 s back-off without spending
    an attempt (`error_code` `root_busy`); the run's event log records
    `{"code": "root_busy", "retryable": true}`. Before, the claim was marked failed for
    good, and another tenant's open handle was enough to cause it.
  - Backup and restore refuse while any handle is open. The `.lock` refusal now reads
    "an open run handle, worker or import holds the root lock".
  - `build_default_orchestrator` and `GraphOrchestrator` close a handle they opened when
    wiring fails, so the lock is not leaked.
  - On Windows the lock uses `LockFileEx`, which has a shared mode; with `msvcrt.locking`
    two handles on one root refused each other. Blocking calls keep msvcrt's 10 s retry
    budget. A conflict maps to typed busy by platform: `ERROR_LOCK_VIOLATION` (33), or for
    an errno-only error `EACCES`/`EDEADLOCK` on Windows and `EAGAIN`/`EWOULDBLOCK`/`EACCES`
    on POSIX; any other lock error keeps its `winerror`. Before, a contended lock on
    Windows raised an untyped `PermissionError` (errno 13), so the bounded wait was zero.
    The lock file stays empty, and a failed `UnlockFileEx` is reported.
  - API startup on Windows (#168, #169): `create_app` takes `api.lock` shared through
    `LockFileEx`, so several API processes on one root can start. With `msvcrt.locking`
    that lock was exclusive, and a second API process failed after 10 s with an untyped
    `OSError`. While backup or restore holds `api.lock`, startup now waits out the 10 s
    budget and raises typed `RootLockBusy`. `api.lock` stays empty (no `\0` byte).
  - Backup no longer archives the root's `.lock` and `api.lock`, and restore does not
    extract them. Both are empty lock targets that backup and restore hold exclusively,
    and Windows byte-range locks are mandatory, so reading or overwriting them failed.
  - Run leases on Windows: `run_leases.lock_dev`/`lock_ino` (diagnostic only) store a
    value outside SQLite's signed 64 bits as hex text. On `windows-latest` the file ID in
    `st_ino` was wider, and every `RunLease.acquire` raised `OverflowError`, so every
    worker run failed there.

  Known limits:
  - Stores opened directly rather than through a run handle are not guarded (worker queue,
    skill index, lineage, eval, run-event and API stores; #159); a worker opens some of
    them before it takes the lock. Neither are processes that ignore the advisory lock.
  - The lock is per runtime root, not per tenant (#158). An open handle on one tenant,
    including a long worker run, makes a pending leftover import on another tenant raise
    `RootBusy` until it closes; on the worker path that claim is re-queued, not failed.
    REC-065 (above) adds per-tenant locks and lifts this limit.
  - `RootBusy` is not yet the `root_busy` API/CLI error (#145): the API returns a generic
    error for it.
  - The leftover path (`migrate=off` with pending files) waited without a bound (#153).
    Since REC-064 (above) it waits at most `RECERTIA_IMPORT_LOCK_TIMEOUT_S` and raises
    typed `RootBusy` on every platform, which both worker paths re-queue.
  - The operations-schema exclusive lock is unbounded and the faithfulness command's
    orchestrator is not closed on every path (#160).
  - Worker imports on older releases lock `<root>/runs/<tenant>/.lock`, so mixed-version
    workers do not exclude each other.

  Tests: `tests/unit/persistence/test_root_lock_writers.py`,
  `tests/unit/persistence/test_root_lock_windows_path.py` (Linux, fake `kernel32`),
  `tests/unit/persistence/test_root_lock_windows_real.py` (real `kernel32`, run by the
  `root lock (windows-latest)` job).

- **psycopg floor (REC-057, #179)** — the `postgres` extra's floor is now `psycopg[binary]>=3.2.1`.
  psycopg 3.2.0's `binary` extra requires `psycopg-binary==3.2.0.dev1`, which was never
  uploaded to PyPI, so `psycopg[binary]==3.2.0` cannot be installed. 3.2.1 (published the
  next day) requires `psycopg-binary==3.2.1`. Found once the nightly lowest arm started
  pinning the `postgres` floor. No other floor changes.

- **CLI tests read stderr at the click floor (REC-053)** — the `dev` extra gains
  `click>=8.2`. Six CLI tests read `result.stderr` from a default `CliRunner()`, and
  before click 8.2 that runner mixes stderr into stdout, so the read raised
  `ValueError: stderr not separately captured` with typer 0.13 and click 8.0.0–8.1.8
  (typer 0.13 accepts `click>=8.0.0`). The floor goes in the `dev` extra only: `src/`
  never imports click, and typer 0.26+ has no click dependency at all (click arrives
  only through uvicorn). With the floor declared, the nightly lowest-deps arm pins
  `click==8.2` and exercises it. `tests/unit/cli/test_exit_codes.py` also asserts
  `LEDGER INVALID:` is in `result.stderr` and not in `result.stdout`, shows
  `result.stderr` when the exit code is wrong, and checks that `raise_exit` raises
  exactly `typer.Exit`. No runtime floor changes.

- **Exit-code test at the typer floor (REC-050)** — `tests/unit/cli/test_exit_codes.py`
  imported `typer.exceptions`, a module that first ships in typer 0.27.2, so it failed
  with `ModuleNotFoundError` at the declared `typer>=0.13` floor. It now catches the
  public `typer.Exit`, which is the class `recertia.cli.exit.raise_exit` raises on every
  typer version (`click.exceptions.Exit` on 0.13, `typer.exceptions.Exit` on 0.27.2+).
  The assertions are unchanged and no dependency floor moves.

- **REL-3 probe temp-dir cleanup hardening (REC-052)** — follows REC-047. `fresh_root` in
  `tests/probes/rel3_probe.py` (now built on an `owned_root()` context manager, also used by
  `rel3_probe_d` and `rel3_probe_busy`) kills and reaps every child the scenario started
  (multiprocessing children and `subprocess_exclusive` lock holders) before it removes the
  dir, and retries the removal once. Before, a pytest-timeout that fired mid-`join()` left a
  child writing into the dir, so `rmtree` failed with `OSError: Directory not empty`, the dir
  leaked and the OSError replaced the timeout. The scenario's own exception now always
  propagates; a removal failure is attached to it as a note. SIGKILL, SIGTERM with no handler
  and `os._exit` still leak the dir by design. Only the scenario's own children, started
  after entry, are reaped (no grandchildren, no process-group kill), and a second interrupt
  during cleanup replaces the first; `fresh_root`'s docstring documents all of this. The
  `subprocess_exclusive` holder now closes its stdout/stderr pipes, and the r3 busy scenarios
  read and append their `.effects` journal with closed files, so the probe tests emit no
  ResourceWarning (16 before). `tests/unit/graph/test_rel3_probe_tmp_cleanup.py` adds
  KeyboardInterrupt and pytest-timeout (BaseException) cases, a still-writing-child case in
  process and under a real pytest-timeout, the one-retry path, the reap scope, a static guard
  that fails on any reference to `mkdtemp` in `tests/probes/` (calls, aliases such as
  `from tempfile import mkdtemp as m` or `mk = tempfile.mkdtemp`, and
  `getattr(tempfile, "mkdtemp")`), and a ResourceWarning check, and is now `ruff format`ted.

- **pytest floor (REC-049)** — `tests/unit/test_faulthandler_backstop.py` reads the
  `faulthandler_exit_on_timeout` ini option with `getini()`, and pytest added that option in
  9.0.0. The `pytest>=8` floor let the nightly `import-all (py3.11 lowest)` job install
  pytest 8.0.0, where the call raises `ValueError: unknown configuration value`. The dev
  floor is now `pytest>=9.0`. No other floor changes.

- **Nightly lowest-deps install (REC-048)** — `import-all (py3.11 lowest)` pins every
  direct dependency at its `pyproject.toml` floor, and the `pytest-timeout>=2.3.0` floor
  from #112 named a version that was never uploaded to PyPI (2.3.1 was the first 2.3.x
  upload), so pip stopped with `ResolutionImpossible`. The floor is now `2.4.0`: the
  backstop tests match the "Timeout (>Ns) from pytest-timeout" failure text that 2.4.0
  introduced, and four of them fail on 2.3.1.

- **Leaked `recertia-neg-*` temp dirs (REC-046)** — sensitivity proofs no longer leave an
  empty `recertia-neg-*` directory behind. `author_sensitivity_proof` takes
  `negative_workdir` as optional; when it is omitted, the criterion runs in a
  `tempfile.TemporaryDirectory(prefix="recertia-neg-")` that is removed before the call
  returns or raises. The critic (`intake`), the success and paper distillers and the mine
  worker now use it, so a full test run leaves 0 such directories instead of 73. The
  critic's negative fixture now always lives in the system temp dir rather than the task
  workdir, so it no longer ends up in workspace snapshots or diffs.

- **Leaked `tmp*` dirs from the REL-3 probes (REC-047)** — the probes in `tests/probes/`
  (`rel3_probe`, `_r3`, `_r4`, `_d`, `_api_busy`, `_busy`) created every scenario root with a
  bare `tempfile.mkdtemp()` and never removed it, so a full test run left 29 `tmp*` dirs and
  3 `tmp*.effects` files in the temp dir. Each scenario now runs in a
  `tempfile.TemporaryDirectory` it owns (the `fresh_root` decorator in `rel3_probe.py`, or a
  `with` block), removed when the scenario returns or raises; the busy scenarios' effects
  journal lives in that directory too. A full run now leaves none.
  `tests/unit/graph/test_rel3_probe_tmp_cleanup.py` runs every probe entry point against an
  empty private temp dir and checks it is still empty afterwards.

- **REL-3 probe_b / probe_a2 hang (REC-017)** — `tests/probes/rel3_probe.py` now
  uses the multiprocessing `spawn` context, so a probe child never inherits
  threads or locks from the pytest process (a fork while a `recertia-queue`
  poller held a SQLite mutex left the child blocked in `sqlite3.connect`). The
  racer barrier waits at most 30s and every child join at most 60s; on a hang
  `_join_or_fail` dumps the parent's stacks (faulthandler), sends SIGUSR1 so
  each child dumps its own, kills the children and fails. A suite-wide
  `pytest-timeout` backstop (`timeout = 60`, `timeout_method = "signal"`) fails
  any test whose setup, call and teardown together run longer than 60s, and
  prints every thread's stack when more than one Python thread is alive. CI's
  `ci` job is capped at 10 minutes and `container-smoke` at 5. Probe `record()`
  assertions are unchanged. The remaining fork-with-threads warnings on CI come
  from grimp's native rayon pool, started by the `tests/boundary` graph build
  on a cold `.grimp_cache`; they are not addressed here.

- **pytest-timeout backstop vs the bounds under it (post-merge #112/#115)** — the
  60s suite timeout pre-empted bounds that were meant to fire first: the REL-3
  probe's join (60s, now 40s; barrier 30s, now 20s), the pytester child limits of
  the teardown-wiring (90s) and exit-guard (60s) tests (now 45s), and two
  subprocess limits (120s and 60s, now 45s). Probe children are daemonic, so a
  pre-empted probe test can no longer block interpreter exit on a hung child. The
  teardown wrapper now also catches `pytest.fail.Exception` (what the signal
  method raises) while stopping workers, so a timeout there no longer skips
  pytest's teardown and cascades. `container-smoke` leaves the timeout test's
  PENDING list now that #112 is on main.

- **faulthandler backstop under pytest-timeout (REC-017, rest of #96)** —
  `faulthandler_timeout = 60` in `[tool.pytest.ini_options]`. pytest-timeout's
  `signal` method runs its handler on the main thread, which never happens while
  that thread is blocked in C (the REC-017 child sat in `pthread_mutex_lock` under
  `sqlite3.connect`), so such a hang printed nothing until the CI job was killed.
  faulthandler's native watchdog dumps every thread's stack at 60s regardless; it
  does not exit (`faulthandler_exit_on_timeout` stays off), so failing the test is
  still pytest-timeout's job. The watchdog is one native thread per running test,
  so a `fork` inside a test would always run in a multi-threaded process.
  `tests/runtime/test_ledger_concurrency.py`, the last test that forked, now
  starts its four appenders with `spawn`, releases them together through a
  barrier, bounds their joins with one 40s deadline and kills stragglers; it also
  asserts that the appends interleave, and a new test holds the file-lock
  registry guard in another thread while two appenders start (a forked child
  would block on it). The suite now reports no fork-with-threads
  `DeprecationWarning` without any warning filter. New test
  `tests/unit/test_faulthandler_backstop.py`.

### Added

- **`v1_error` takes an optional `retry_after_s` (#145)** — a non-null value sets `Retry-After`
  in whole seconds, rounded up, like `recertia_error_response`; null (the default) sends no
  header. Both now build the value in one helper, `retry_after_header`, which never raises: a
  `nan`, an infinity, a negative number, a bool, a non-number or a value above `2**31 - 1`
  seconds sends no `Retry-After` header and keeps the response status. Before, `nan`
  raised `ValueError` and an infinity raised `OverflowError` in `recertia_error_response`
  (a 500 in place of the 429/503), and a negative value sent an invalid header. No raise site
  sets a value yet.

- **Windows root-lock CI job (REC-058)** — `ci.yml` gains a separate `root lock
  (windows-latest)` job that installs the package on `windows-latest` (Python 3.12) and
  runs `python -m pytest tests/unit/persistence -k root_lock -rs -v -o
  timeout_method=thread`, so the root lock is exercised against the real `kernel32`. Any
  `tests/unit/persistence/test_root_lock_*.py` file is picked up by name. A new `windows`
  pytest marker labels tests that need real Windows; a second step runs
  `pytest tests/unit/persistence -m windows` with its own minimum
  (`WINDOWS_MARKED_MIN_PASSED`, 14), so a marked test outside the `root_lock` name
  selection still runs. The job fails on any failed, errored or skipped test (xfail
  included) in either step, on fewer passes than `ROOT_LOCK_MIN_PASSED` (73) or
  `WINDOWS_MARKED_MIN_PASSED`, and on a missing or empty JUnit report. Each guard also
  fails when its minimum is below 1 or not an integer, and on success prints a
  `guard: {...}` summary. Markers are now strict (`addopts = ["--strict-markers"]`), and
  so are xfails (`xfail_strict = true`: an unexpected pass fails), and the
  Linux `ci` job's pytest prints skip reasons (`-rs`). It has a read-only token, no
  secrets, `checkout` with `persist-credentials: false`, and actions pinned to the same
  SHAs as `weekly-ops.yml`. The `ci` job and the required check are unchanged.

- **REC-011 C1 part 1 run resolution** — `EvalStore.append_run` records
  `resolution` (`decided` | `unresolved` | `invalid`), `exit_code`, and
  `reason`. Exit 124/126 on a required criterion is unresolved;
  `terminal=error` is invalid and keeps the criterion `exit_code`; otherwise
  decided. Pre-existing rows stay SQL `NULL` and report as `legacy`. Recording
  only: `arm_counts`, lift, gates, and promotion do not read the field. The
  Eq. 5 envelope is a later commit (T4 base: field exists, no envelope).

- **REL-5a `commit_hop`** — a hop writes the checkpoint, `trajectory_events`,
  `route_entries`, and generic `outbox` rows in one fenced SQLite transaction.
  `checkpoint.before_commit:kill` cannot leave a taped event without its
  checkpoint; resume event count matches committed hops (F16). The JSONL tape
  is a projection of `trajectory_events` after commit. Eval observation
  delivery (REL-5b) and quota/affordance/program (REL-5c) stay on later slices.

- **REL-4 durable RunQueue** — `POST /v1/runs` and resume enqueue onto
  `control.db` `run_queue`. The request thread never calls
  `orchestrator.start`; an embedded drainer (or `recertia worker`) claims
  fairly across tenants. `RECERTIA_API_DEFAULT_ASYNC` stays `0`, so omitted
  `mode` enqueue-and-waits (default 60s) and returns 202 + `Retry-After` on
  timeout. In-flight is `COUNT` of claimed/running rows, so a leaked
  `quotas.sqlite` gauge cannot 429 a tenant after restart. Cancel is checked
  at every hop boundary. Eval/job kinds still run on the request thread.

- **SEC-7 PDP.require** — `PolicyDecisionPoint.decide` / `require` implement the
  action table (`runs.create`, `runs.read`, `runs.script`, `runs.cancel`
  other-tenant → 404, `workspace.register` / `skill.promote_seed` as
  `platform_admin`). `RunService.create` / `resume` / `get` are the first
  consumers. `eval.run` still accepts `metrics` or `evals` until a distinct
  evals key scope is issued.
- **Store Receipts P1** — `{runs_root}/receipts.jsonl` dual-write next to the
  ledger, `recertia receipt show/verify/close` (advisory verify; missing genesis
  exits 0). Tabletop-from-tar and `backup --receipts` defaults unchanged.
- **ADR-0020 Store Receipts (Proposed)** — `StoreReceipt` contract, `close_receipt`
  ledger action, `schema/store_receipt.schema.json`. Does not flip `recertia backup`
  or tabletop defaults. P4 may switch `backup --receipts` only after one counted
  soak week compares tar vs receipt restore. Plan:
  [`docs/plans/2026-09-23-store-receipts.md`](docs/plans/2026-09-23-store-receipts.md).

- **F9 RunRegistry** — durable `(tenant_id, run_id)` reservation in
  `runtime.db` via `RunRegistry.reserve`. `POST /v1/runs` rejects reuse with
  HTTP 409 across process restarts (`run_id_exists`).

- **F11 WorkspaceLock** — flock under `locks/workspaces/<sha256(workdir)>.lock`.
  `create_run` and `resume_run` acquire after workdir resolve; a second holder
  gets HTTP 409 `workspace_busy`.

### Fixed

- **CI evidence allocation hang** — `test_version_allocation_is_safe_across_processes`
  reaps leftover workers and reports via files instead of `multiprocessing.Queue`,
  so a failed allocation cannot orphan pytest. The `ci` job has a 25-minute
  timeout so a stuck suite cannot run to the GitHub 6-hour cap.
- **F7 live operation reclaim** — `OperationLedger.run_once` waits on a live
  in-process owner instead of reclaiming a pending row after 5s. A crash-left
  pending sentinel with no holder is still claimed immediately.
- **F10 scratch resume** — `solve_scratch_observe_act` records the model command
  inside `op_once`, so resume replays the stored command instead of asking the
  model again.
- **SEC-5 model-origin on local** — `LocalExecutor` refuses `CommandRequest.origin`
  `model`/`skill` (defense in depth if a caller bypasses `shell_handler`).
  `agent_subtask` stamps `origin="model"` on the request it executes.
- **SEC-8 require_key** — `require_key` uses `Principal.has_scope`, so tenant
  `admin` no longer satisfies `platform_admin`.
- **Windows `rss_bytes`** — `import resource` is POSIX-only. Catch `ImportError`
  so hop telemetry on native Windows returns `0` instead of aborting the graph
  (`ModuleNotFoundError: No module named 'resource'`).
- **CI architecture2 drift** — regenerate `docs/architecture2.md` so the Recuris
  `a10` assumption from #47/#48 is compiled. `generate_architecture2.py --check`
  is green again.
- **weekly-ops postgres-soak** — use `pgvector/pgvector:pg16` instead of
  `postgres:16-alpine`. Soak migrations call `CREATE EXTENSION vector`; the
  stock image does not ship it, so every Monday job failed.

### Changed

- **REL-3 R4 probes rewritten to typed `RootBusy` (REC-056b2, #139; ADR-0022)** — the six
  REL-3 probes that were skipped on main (I, L, M, M2, T1, T2) now run. They expected an open
  under the held exclusive root lock to succeed and overlay the leftover `checkpoints.db` /
  `operations.db`, which contradicted the green R3 probes and the shipped contract.
  [ADR-0022](docs/adr/0022-rootbusy-no-leftover-overlay.md) keeps main's behaviour: on the
  leftover-import path, an open that finds the lock held raises typed `RootBusy` within
  `RECERTIA_IMPORT_LOCK_TIMEOUT_S` and writes nothing; there is no overlay, no same-thread
  holder exception and no lock identity. The six probes assert exactly that (new
  `busy_open` helper in `tests/probes/rel3_probe.py`: typed `RootBusy` within
  `BUSY_BOUND_S`, no root-level db file changed), plus convergence after release: the
  first open imports once, I lists exactly the leftover run and the retried writer's run,
  M keeps leftover seqs 1–3 exactly, T1 does not re-execute the imported op, and T2 loses
  nothing. L and M are renamed to match. Their node IDs are unchanged, the 12 O/M3/M4
  probes, T3–T5 and `tests/unit/persistence/test_import_legacy.py` are untouched, and
  `tests/unit/graph/test_rel3_probe.py` has no unconditional skip left. Test-only: no
  `src/` change.
- **Nightly runs the full suite (REC-051)** — both `nightly.yml` arms (py3.11 at the
  lowest-direct floors, py3.13 at newest) now run `pytest -v` the way `ci.yml` does, with
  the same env and the suite-wide pytest-timeout, after the import-all step. The job
  timeout is 20 minutes.
- **Nightly extras, checkout token, failure alert (REC-057, #180)** — both `nightly.yml`
  arms now install the `postgres`, `pdf` and `otel` extras along with `dev`, and the lowest
  arm pins their floors too (psycopg 3.2.1, pypdf 4, opentelemetry-api 1.20). The pin
  script drops a requirement's extras from its constraint and stops on a marker or a
  conflicting floor. `actions/checkout` runs with `persist-credentials: false`. A new
  `alert` job runs only when a scheduled run does not succeed (it fails, is cancelled or
  is skipped): it opens one issue titled "nightly: scheduled run failing" (found by exact
  title, bot author and a marker line), or comments on it if it is already open,
  and skips a run id it already reported.
  The body is run metadata only. The job has no checkout and no `uses:` actions. It is the
  workflow's only write grant (job-scoped `issues: write` on `GITHUB_TOKEN`); the
  workflow-level `permissions` is now `{}`.

- **Nightly alert and extras hardening (REC-062, #180)** — follow-up to the first
  nightly-extras pull request (#143, which was closed and superseded).
  The alert job's run-id dedupe now counts only comments by `github-actions`, so a
  visitor's comment carrying the run-id key cannot suppress an alert. Its condition is
  `always() && github.event_name == 'schedule' && needs.import-all.result != 'success'`,
  so a job that hits `timeout-minutes` (reported as cancelled) also alerts. A new
  `Extras import` step imports psycopg, pypdf and opentelemetry and fails unless
  psycopg uses the `binary` implementation, so dropping `[binary]` is loud.
  `NIGHTLY_EXTRAS` gains `api`, so the pin script's conflicting-floor check now
  compares the `api` and `dev` floors. The script matches names by PEP 503
  normalisation. Comments note the 60-day schedule auto-disable, the 100-item `gh`
  fetch limit and the psycopg 3.2.1 wheel range (cp312 at most).

- **REL-3 runtime.db** — `open_run_persistence` imports leftover
  `checkpoints.db` / `operations.db` into `runtime.db` when
  `RECERTIA_RUNTIME_DB_MIGRATE=auto` (default), then copies the sources to
  `*.db.migrated` without renaming them away. Import completion is a
  `schema_migrations` row (`runtime.import_legacy_checkpoints`), not leftover
  file presence. Anyone who finds exclusive `.lock` already held waits,
  then raises typed `RootBusy` and must not write somewhere else (no
  same-thread holder exception; `.lock` stores no pid/tid identity). After
  the holder releases, a fresh open imports leftover plus the holder's
  writes; unequal leftover checkpoints keep dest and still import other
  seqs. Leftover writers (`migrate=off` handles) hold the root lock and their
  tenant lock shared, so an import of the same tenant, which takes its tenant
  lock exclusive, cannot race them. An older release's handle holds only the
  root lock shared and does not keep that import out (see the upgrade
  requirement under REC-065 above).
  `off` keeps unused legacy files for one release; after an import has
  landed, `off` reads `runtime.db` so post-import writes cannot vanish.
  `manual` refuses to start while leftover files are still pending
  (`state_corrupt`); only once no process of an older release uses this root,
  reopen with `RECERTIA_RUNTIME_DB_MIGRATE=auto` to import them (retry on
  `root_busy`), otherwise stay on `RECERTIA_RUNTIME_DB_MIGRATE=off`. Backup and
  restore take exclusive `api.lock` then `.lock`.
  Pending operation sentinels are dropped on import. Readers (list, tabletop,
  operator brief) prefer `runtime.db`.
- **REL-3 FencedRunPersistence** — checkpoint, operation, and hash-chain
  writes compare the caller's fencing token to `run_leases`. A stale token
  raises `LeaseLost` (409, exit 6). `CheckpointStore.save` re-checks the
  token inside the same `BEGIN IMMEDIATE` as the `INSERT` (ATTACH when
  `runtime.db` is a second file). A missing lease database fails closed.
  Hash-chain appends that target a held `run_id` require a matching token;
  skill-id targets with no lease row still append. Token-less writes are
  allowed only for a run that has **never been leased** (no `run_leases`
  row). A held or released run is fail-closed, token-less included; the
  released holder's last token is dead. Re-acquire issues a new token.
  The ledger compare runs in the same `BEGIN IMMEDIATE` as the JSONL
  append.
- **REL-3 run_leases** — `RunLease` records a row in `runtime.db`. A crash that
  leaves a HELD row on the same lock inode is a resume (incarnation stays,
  fencing token advances) so finished operations are not re-run. A clean
  `release` marks the row released and keeps incarnation/token so they cannot
  go backwards. Steal is an expired heartbeat plus a new lock inode (unlink
  while live is `RunBusy` until TTL). Heartbeat uses `time.monotonic()` plus
  wall clock. Tokens are seeded from the row, so wiping `locks/` cannot
  restart at 1.
- **REL-3 incarnation** — operation identity includes `incarnation`. Legacy
  `operations.db` files are rebuilt onto the new primary key on open. The
  rebuild is one SQLite transaction under the runs-root lock and is resumable
  if `operations_rel3` was left behind by a crash. Duplicate `put()` raises
  `OperationConflict` rather than `INSERT OR REPLACE`, so a recorded result
  cannot be silently overwritten (`run_once` is the idempotent path). The
  file-lock `RunLease` keeps incarnation at 1 so resume after a clean close
  still replays ops; steal bumps wait on `run_leases` rows.
- **ARCH-5 PR-5.5 AppState and CLI exits** — `ConsoleContext` is gone. `create_app`
  stores one `AppState` on `app.state.recertia`. HTTP handlers live in
  `api/routes/*.py` and share `api/schemas.py`. CLI errors go through
  `cli/exit.py::raise_exit` (A2 table: not found 1, invalid 2, auth 3, quota 4,
  conflict 5, busy 6, unavailable 7). `ledger verify` stays 0 or 2. URL paths
  and JSON keys stay.
- **ARCH-5 PR-5.4 library, programs, workspaces, keys, ops** — remaining HTTP
  routes and CLI commands delegate to `LibraryService`, `ProgramService`,
  `WorkspaceService`, `CheckpointReader`, `KeyService`, and `OpsService`.
  `pdp.require` covers `skills.read`/`search`, `programs.*`, `workspaces.*`,
  `proposals.*`, `policy.read`, `ledger.verify`, `blobs.*`, `keys.*`,
  `tenants.switch`, and `ops.*`. URL paths and JSON keys stay. `ledger verify`
  still exits 0 or 2. ConsoleContext remains until PR-5.5.
- **ARCH-5 PR-5.3 promotion and evals** — `PromotionService.promote` runs the
  golden gate through `GateRunner`. `EvalService` owns `POST /v1/evals/runs`,
  `GET /v1/metrics/report|canary|dashboard`, and the CLI `skills promote`,
  `eval run`, `canary`, `metrics`, `lift`, and `probes run` paths.
  `pdp.require` uses `skills.promote` and `evals.run` / `metrics.read`.
- **ARCH-5 PR-5.2 job registry** — `JobService` is the one name table (`default_job_specs`).
  `recertia jobs run` and `POST /v1/jobs/{job}/run` call `JobService.run` with
  `pdp.require(jobs.run)`. Alias chains (`mine|miner`, `hex|practice_hex`, …)
  no longer live in the CLI and HTTP dispatchers.
- **ARCH-5 PR-5.1 run admission** — `POST /v1/runs`, `GET /v1/runs/{id}`, and
  resume call `RunService.create` / `get` / `resume`. Quota admit and
  `pdp.require` (`runs.create`, `runs.script`, `runs.resume`, `runs.read`) live
  in the service. HTTP handlers map errors; they do not call
  `orchestrator.start` or `quota.admit`.
- **Nightly lowest-deps floors** — raise direct floors so `pip install -e ".[dev]" -c lowest-direct.txt` can run the suite: `pydantic>=2.11,<3` (schema output), `typer>=0.13` (click 8.3+ secondary flags), `jsonschema>=4.0.1` (4.0.0 is yanked). Do not loosen the nightly lowest-deps job (#72).
- **Remaining-work inventory** — fold RW-SR, RW-RC, and RW-REM (ADR-0021 Phase 0–1 shipped, Phases 2–5 remaining) into [`docs/architecture/remaining-work.md`](docs/architecture/remaining-work.md) §2/§15. Architecture index no longer calls the remediation plan "specifications only".

- **Console / registry / distill extract** — `register_console_routes` is a
  dispatcher over session / library / program modules (`console_deps.RouteState`
  owns tenant/auth helpers). First-domain tool handlers live in
  `recertia.solver.handlers`; `default_registry` only registers. Success, import,
  and paper distill share `assert_candidate_hygiene` / `assert_non_noop_skill`
  (true-noop steps and `true`/judge-only criteria refused; import/paper still
  `require_clean`). Not a GA gate; no 16th graph node; job CLI/HTTP dispatch is
  unchanged.

- **RW-OR honesty** — remaining-work §8 and the milestone map mark OR1–OR3 as shipped (OG-7…OG-11 tests already on main). Unknown-slug cost uses defaults and is not vendor-exact.

- **2026-08-24 landing extract** — one owner per seam: `ingest_trajectory` /
  `ImportResult.as_public_dict` (CLI + `POST /v1/trajectories/import`),
  `skill_task_class` (snake Goal/golden → kebab `SkillVersion.task_class`),
  imported distill hygiene + true-noop refusal, `ExactMatchTtl` for read-only
  caches (writes still skipped), `hop_finished_attrs` hop gauges, systems/MEA/
  operator-brief `not_established` language. Import still never promotes;
  distill stays candidate-only; MEA and idle offload stay default-off; prefix
  tree stays a view; no 16th graph node. Promotion-with-lift and a live computer
  backend remain open. Not a GA gate.

### Added

- **Trajectory distill HTTP twin** — `POST /v1/trajectories/distill` shares
  `distill_imported` / `as_public_dict` with `recertia trajectory distill`.
  Candidate only; `promoted` is always false. Not a GA gate.

- **arXiv ingest in architecture2** — `docs/architecture/arxiv-ingest.md` is
  compiled into `docs/architecture2.md` (same order as `architecture.md`).


- **2026-08-24 landing extract prompt** — executable Cloud Agent prompt + ADR-0014
  Goal pack for the dual-path extract after today's MEA / trajectory / systems stack.
  Not a GA gate. Plan:
  [`docs/plans/2026-08-24-todays-work-refactor.md`](docs/plans/2026-08-24-todays-work-refactor.md).

- **Systems residency (ADR-0018)** — hop telemetry emits `component_class`,
  `rss_bytes`, `workdir_bytes`, `idle_gap_ms` on `node.finished`; canonical keys
  on `tool.invoked` / `retrieve.queried`; `model.completed` from `ModelClient.complete`.
  Read-only tool-result and retrieval caches (writes never cached; index rebuild
  flushes). Idle workdir offload is hash-checked and **default off**
  (`state_management.idle_offload_enabled`). Prefix-tree is a derived view over
  existing trajectory JSONL. `recertia systems` prints the six-property snapshot.
  Fixture baseline is not a 4.6× claim; golden-class RSS is still unmeasured.

- **External trajectories (ADR-0019 rewrite)** — binds to the shipped
  `TrajectoryImport` contract (ProvenanceBundle from `audited_task_state`, snake
  `bug_reproduction` / `playtest_operator` / `docs_auditor`). No
  `Policy.external_trajectory_import` flag. `recertia trajectory distill` authors
  a **candidate** only (kebab `SkillVersion.task_class`). `POST /v1/trajectories/import`
  is the HTTP twin of the CLI. Reexecutable imports with a criteria snapshot
  queue a *pending* proposal, never approved. `external_computer` is registered
  as an `external` tool and refuses unless isolation flags + allow-list are set;
  even then this build does not open a standing VM. JobQuota
  `computer_use_practice_share` (default 0.15). `recertia systems --brief` prints
  stuck/lift/redundancy with honest "not established". Goldens stay eval-firewalled
  and off the repo-chore promotion gate.

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
  promotion gate. Candidate distill, HTTP import, and ADR-0019 landed in
  the Unreleased ADR-0019 rewrite bullet; promotion-with-lift and a live
  computer backend remain open. Plan:
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

[0.4.0]: https://github.com/recertia/recertia/compare/0.3.0...0.4.0
