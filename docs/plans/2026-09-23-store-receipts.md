# Store Receipts — all-phase plan (2026-09-23)

**Date:** 2026-09-23  
**Status:** proposed (ADR-0020). Not a GA gate.  
**Decision:** [`docs/adr/0020-store-receipts.md`](../adr/0020-store-receipts.md)  
**Inventory:** RW-SR in [`docs/architecture/remaining-work.md`](../architecture/remaining-work.md)  
**Contract:** [`contracts/receipt.py`](../../contracts/receipt.py)

This file documents every phase so later PRs do not invent a second architecture.
It is not a merge gate. Remaining-work is the standing plan.

## Invariant

`store` publishes a receipt. `.recertia/` is a checkout. Residue that never earns a fold is not in the backup set.

Runs do not close receipts (ADR-0017). Curator / sleep / `recertia receipt close` do.

## Phase map

```text
RW-GA soak / live traffic / tabletop-from-tar     unchanged
P0 ADR-0020 + StoreReceipt + close_receipt        this PR; Proposed
P1 receipt index inside existing tar              parallel with RW-GA
P2 fold SQLite                                    after P1 green; finishes ADR-0007
P3 residue packs                                  after P2 dual-read
P4 backup --receipts + tabletop-from-receipt      after one soak week compares both
P5 sleep closer + spectrum + validity             after P4 soak row; HEX still gated
```

## P0 — documents and contract (this PR)

- ADR-0020 Proposed.
- `StoreReceipt` + `LedgerAction.close_receipt` + generated schemas.
- RW-SR row. This plan.
- Do **not** flip go-live.md or tabletop restore semantics.
- Spec amendment allowed: add `close_receipt` to the §21 action list so the documented enum matches the contract.

**Done when:** `python3 scripts/generate_schemas.py --check` is green; cross-refs pass; ADR is Proposed.

## P1 — receipt as an index, layout unchanged

On each existing `store` path and each Curator pass, append `close_receipt` whose residue is a manifest of current paths and content hashes (no pack yet). `reviewed_git` is `HEAD` of the repo that owns `skills/`. `fold_digest` may be null.

`backup_tree` embeds `receipts.jsonl` in the existing tar. `recertia receipt show` / `verify` walk the chain. Tabletop verifies the chain after restore.

Genesis: first receipt after upgrade inventories the existing tree or restore is a lie.

**Done when:** `recertia ledger verify` still passes; planted lift goldens unchanged; tabletop from the current tar still works; a receipt with empty `compression_moves` is valid.

**Does not:** delete files, change retrieve, enable offload, enable HEX.

## P2 — fold is SQLite

Move `SkillStatus` event log, `SkillStats` rows, `CaseRecord`, and `edges` into one `.db`. Dual-read: retrieve and CLI accept SQLite or JSON files. Curator migrator is dry-run first, then write.

`close_receipt` sets `fold_digest` to the hash of `VACUUM INTO` (or SQLite backup API) taken at close. Status remains append-only events plus a projection. Stats remain T0 / rebuildable.

**Done when:** `recertia cases` / `skills` / metrics report identical answers from SQLite and from the old files on a fixture library; migrator is reversible; `fold_digest` recomputes.

Do not delete JSON dirs in this phase. No new specifications topic.

## P3 — residue is a pack

New writes from `TranscriptStore`, `FilesystemBlobStore`, and `WorkspaceManager.snapshot` land in a packfile. Readers accept pack *or* loose file. `snapshot_ref` becomes a tree or tarball CID. Windows: pack is a file; no reflink required. The `WorkspaceManager` class comment already allows swapping `copy2` trees.

Offline `recertia gc pack` migrates existing loose objects. Do not delete loose files until P4.

ADR-0018 stays default-off. Its handle shape (path, hash, bytes) is the pack handle. A CID mismatch on restore is `RoutingError`.

**Done when:** a new run produces no new loose blob/transcript/snapshot files under `.recertia/`; restore of `snapshot_ref` from a pack is hash-identical; `residue_cid` on the next receipt names that pack.

## P4 — backup copies receipts (durability unit flips)

`backup_tree` gains `--receipts` (default off): ledger + git bundle of reviewed memory + fold snapshot + residue packs. `recertia tabletop --restore-from` accepts a receipt directory as well as a tar.

Keep the full-tree tar until **one counted soak week** restores both ways with recorded TTR. Then, in the same PR:

1. Flip `--receipts` to default.
2. Promote ADR-0020 to **accepted**.
3. Amend operator sentences that name the tree as the unit:
   - [`docs/architecture/go-live.md`](../architecture/go-live.md) backup / RPO row
   - [`docs/architecture/incident-tabletop.md`](../architecture/incident-tabletop.md)
   - backup/restore help text
   - remaining-work RW-GA tabletop line (accept receipt dir *or* tar)

Only after that flip may GC delete loose files that a receipt no longer cites, using `recertia gc --older-than-days` composed with "uncited by the last N receipts."

**Done when:** tabletop-from-receipt equals tabletop-from-tar on a recorded soak week; backup artifact count is the spine plus a handful of files; `ledger verify` covers receipts; RPO ≤ 24h.

This is the only phase that writes a specifications / go-live durability-unit change.

## P5 — sleep closes the receipt; spectrum and validity

Do not start until P4 has a soak row and RW-HEX predicates are still respected (`practice_conversion` is a number; `causal_lift` interval exists).

Curator becomes the receipt closer: pack new residue, snapshot the fold, GC uncited residue. `compression_moves` only when faithfulness / control-arm evidence already exists. Sleep never writes `SkillVersion`.

Then, separately so it cannot block backup:

- `valid_from` / `valid_to` on fact and case rows; retrieve defaults to current.
- Demotion is a `close_receipt` with `compression_moves.delta = -1`. ADR-0006 evidence floor still applies.
- Split transcripts into tool-turn provenance units *inside* residue. Not a new store.
- SkillVersion commits stay incremental in git. ADR-0017 budget still applies.

`curator_compress` and HEX stay false until their existing predicates fire.

**Done when:** a sleep/Curator pass closes a receipt without a user-facing run; demotion leaves the git object reachable; retrieve precision@3 is unchanged or reported with a Wilson interval; HEX flags still false unless RW-HEX predicates hold.

## Success (honesty dialect)

- Tabletop from a receipt succeeds on a counted soak week (P4).
- Lift goldens and the planted canary do not regress. `causal_lift` stays a number or `not established` — never a silent zero.
- Faithfulness on compacted cases stays at the current writer threshold.
- `recertia ledger verify` includes receipts.
- Nightly backup after P4 is the receipt spine plus cited objects. RPO ≤ 24h.
- Retrieve precision@3 is unchanged by P1–P4. P5 may change it only with a Wilson interval.
- Do not advertise live-tree inode wins during dual-write (P1–P3).

## Risks

1. Per-run receipts explode the ledger — close on Curator / sleep / explicit command only.
2. Genesis receipt without a full inventory makes every later restore partial.
3. Dual-write makes disk worse before it gets better.
4. `reviewed_git` on Windows is the repo that owns `skills/`, not a copied workdir. Drive-letter roots only.
5. Hash mismatch is a hard fail, not a skip (ADR-0018).
6. Empty `compression_moves` must be legal or P1–P4 wait on lift evidence Recertia does not yet have.

## Non-goals

Neo4j, Graphiti, A-MEM neighbor rewrite, KV persistence, weight updates, ReFS requirement, retrieve ranking rewrite, enabling ADR-0018 offload, enabling HEX/compress, a sixteenth graph node, marking `a1`/`a2`/`a4` `supported` from CI.
