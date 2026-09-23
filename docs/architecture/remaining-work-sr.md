# RW-SR — Store Receipts (ADR-0020)

Sibling to [`remaining-work.md`](remaining-work.md). Fold into that file §15 in a PR that also regenerates [`architecture2.md`](../architecture2.md).
Do not treat this file as authorization to flip backup or tabletop defaults.

Decision: [`docs/adr/0020-store-receipts.md`](../adr/0020-store-receipts.md)  
Plan: [`docs/plans/2026-09-23-store-receipts.md`](../plans/2026-09-23-store-receipts.md)  
Contract: [`contracts/receipt.py`](../../contracts/receipt.py)

## Inventory row (for remaining-work.md §2)

| ID | Kind | Item | Status |
| --- | --- | --- | --- |
| **RW-SR** | engineering | Store Receipts — published durability unit (ADR-0020) | P0 this PR; P1–P5 open; not a GA gate |

## Phase done-whens

| Phase | Done when | Durability unit |
| --- | --- | --- |
| **P0** | ADR Proposed; `StoreReceipt`; `LedgerAction.close_receipt`; generated schemas; this row | tree (unchanged) |
| **P1** | `receipts.jsonl` inside the existing tar; empty `compression_moves` valid; tabletop-from-tar unchanged | tree + receipts.jsonl |
| **P2** | Fold SQLite dual-read; `fold_digest` is the close-time snapshot hash; JSON dirs not deleted | unchanged |
| **P3** | New residue writes go to a pack; readers accept pack or loose; ADR-0018 stays default-off | unchanged |
| **P4** | `backup --receipts` default-off until one counted soak week compares both; then flip, accept ADR, amend go-live/tabletop/RW-GA. Only then GC uncited loose files. RPO ≤ 24h | **flips** |
| **P5** | Curator/sleep closes receipts; `compression_moves` need faithfulness/control-arm evidence; HEX still gated | receipt spine |

## Invariants

- Runs do not close receipts (ADR-0017).
- Empty `compression_moves` is valid.
- Hash mismatch on restore is `RoutingError`.
- Genesis after upgrade inventories the existing tree.
- RW-GA soak / live traffic / tabletop-from-tar stay the ops gate until P4.
- JSON-in-git remains the reviewed `SkillVersion` / facts / policy path (ADR-0007).

## Non-goals

Neo4j, Graphiti extraction, A-MEM neighbor rewrite, KV-cache persistence, weight updates, ReFS requirement, retrieve ranking rewrite, enabling idle offload, enabling HEX/compress, a sixteenth graph node.
