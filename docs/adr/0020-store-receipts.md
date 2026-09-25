# ADR-0020: Store Receipts — the published durability unit

- **Status:** proposed
- **Date:** 2026-09-23
- **Related:** [ADR-0004](0004-offline-improvement-plane.md), [ADR-0007](0007-skill-identity-status-and-stats-split.md), [ADR-0009](0009-contracts-as-code.md), [ADR-0011](0011-trajectory-and-counterfactual-replay.md), [ADR-0017](0017-version-write-budget.md), [ADR-0018](0018-idle-state-offloading.md)
- **Plan:** [docs/plans/2026-09-23-store-receipts.md](../plans/2026-09-23-store-receipts.md)
- **Inventory:** RW-SR in [remaining-work.md](../architecture/remaining-work.md)

## Context

Operator docs currently treat the durability unit as the entire `.recertia/` tree.
`recertia backup` / `scripts/backup_recertia.py` tar that tree. `recertia tabletop --restore-from` unpacks it.
The live tree is also the working set: loose JSON cases, hash-addressed blobs, and `WorkspaceManager.snapshot` `copy2` directories.
A file is treated as durable because it exists. That is why backups accumulate many small files, and why restore copies isolation state that ADR-0018 already classified as residency, not memory.

ADR-0007 already split immutable `SkillVersion` (JSON in git) from rebuildable `SkillStatus` / `SkillStats`.
[operations.md](../architecture/operations.md) already names SQLite for status, stats, and cases.
ADR-0004 already named the offline closer (Curator / sleep).
ADR-0011 already named tabletop replay.
None of those ADRs change what `backup` is allowed to drop.

## Decision

1. The published durability unit is a `close_receipt` ledger entry plus the objects it names (`reviewed_git`, `fold_digest`, `residue_cid`).
2. `.recertia/` is a checkout of that receipt. Presence of a file does not make it durable.
3. Runs do not close receipts. Curator, sleep, or `recertia receipt close` do. Empty `compression_moves` is valid.
4. Hash mismatch on restore is `RoutingError`. Genesis after upgrade must inventory the existing tree.

Promote this ADR to **accepted** only before Phase 4 flips `backup --receipts` to default, or before GC may delete uncited loose files. Those two acts change what a restore is allowed to drop.

## What this does not decide

Pack codec, dual-read windows, `VACUUM INTO` versus the SQLite backup API, when to delete loose files, validity-window column layout, provenance-unit split, ACE-shaped skill diffs. Those live in remaining-work and `contracts/receipt.py` (ADR-0009).

Do not re-decide ADR-0007 (fold rows), ADR-0004 (sleep is the closer), ADR-0011 (tabletop restores named state), ADR-0017 (no per-hop receipt), or ADR-0018 (offload remains residency, default-off).

## Alternatives considered

- **Keep tar-of-tree.** Rejected as the long-term unit: it backups working-set leftovers and scales with inode count.
- **Finish ADR-0007 SQLite and leave backup as a directory copy.** Necessary but insufficient; the published unit would still be the tree.
- **Pack blobs but keep "the tree is the backup."** Relocates the metadata tax; restore still copies what was never folded.
- **Close a receipt per run or hop.** Fights ADR-0017 and explodes the ledger.

## Consequences by phase

Phases are engineering increments, not new decisions. Full done-whens: [the plan](../plans/2026-09-23-store-receipts.md).

| Phase | What lands | Durability unit | ADR status |
| --- | --- | --- | --- |
| P0 | This ADR + `StoreReceipt` + `close_receipt` | unchanged (tree) | proposed |
| P1 | Receipt index inside the existing tar | unchanged (tree + receipts.jsonl) | proposed |
| P2 | Fold SQLite (ADR-0007 debt) | unchanged | proposed |
| P3 | Residue packs; readers accept pack or loose | unchanged | proposed |
| P4 | `backup --receipts`; tabletop-from-receipt | **flips** after one soak week compares both | **accepted** in the same PR |
| P5 | Sleep closer; spectrum moves; validity windows | receipt spine | accepted; HEX/`curator_compress` still gated |

### P0

`contracts/receipt.py` is the structural source. `LedgerAction` gains `close_receipt`.
Do not hand-edit `schema/`.

### P1

Append `close_receipt` on existing `store` / Curator paths. Embed `receipts.jsonl` in the current tar.
`fold_digest` may be null. `residue_cid` may be a path-and-hash manifest. Dual-write makes disk use go *up*.
Do not advertise inode wins. RW-GA tabletop-from-tar stays the ops gate.

### P2

`SkillStatus` event log, `SkillStats`, `CaseRecord`, and an `edges` table move into one `.db`.
Dual-read. `fold_digest` becomes the hash of a SQLite backup snapshot taken at close.
JSON dirs are not deleted in this phase.

### P3

New writes from transcript / blob / workspace snapshot land in a packfile.
`snapshot_ref` becomes a CID. Windows needs no reflink. ADR-0018 stays default-off.
A CID mismatch is `RoutingError`.

### P4

`backup_tree --receipts` copies ledger + git bundle of reviewed memory + fold snapshot + residue packs.
Tabletop accepts a receipt directory as well as a tar.
Flip the default only after one counted soak week restores both ways with recorded TTR.
Then this ADR becomes accepted, and go-live / backup / tabletop sentences that name the tree as the unit are amended in the same PR.
RPO stays ≤ 24h.

### P5

Curator becomes the receipt closer. `compression_moves` only with faithfulness / control-arm evidence.
Sleep never writes `SkillVersion`. Distill → review → store remains the only path into `reviewed_git`.
`valid_from` / `valid_to` on fact and case rows; retrieve defaults to current.
Demotion is a `close_receipt` with `compression_moves.delta = -1`; the git object is superseded, not deleted.
`curator_compress` and HEX stay false until existing RW-HEX predicates fire.

## Non-goals

Neo4j, Graphiti extraction, A-MEM neighbor rewrite, KV-cache persistence, weight updates, ReFS requirement, retrieve ranking rewrite, enabling idle offload, enabling HEX/compress, a sixteenth graph node.
