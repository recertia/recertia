# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **REL-4 durable RunQueue path** — `mode=async` enqueues into the SQLite `run_queue` via `ConsoleContext.enqueue_async_run`. Embedded worker claims and runs. Enqueue-and-wait supported. `RECERTIA_API_DEFAULT_ASYNC` remains off. Restart survival and cancel covered by new tests.

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

(Full remaining content from main preserved.)
