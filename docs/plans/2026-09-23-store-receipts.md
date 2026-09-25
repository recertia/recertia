# Store Receipts — all-phase plan (2026-09-23)

**Date:** 2026-09-23  
**Status:** proposed (ADR-0020). Revised after architect review. Not a GA gate.  
**Decision:** [`docs/adr/0020-store-receipts.md`](../adr/0020-store-receipts.md)  
**Inventory:** RW-SR in [`docs/architecture/remaining-work-sr.md`](../architecture/remaining-work-sr.md) (fold into `remaining-work.md` only with an `architecture2.md` regen)  
**Contract:** [`contracts/receipt.py`](../../contracts/receipt.py)

This file documents every phase so later PRs do not invent a second architecture.
It is not a merge gate. Remaining-work is the standing plan. Do not flip
`go-live.md` or tabletop defaults in P0–P3.

## Invariant

`.recertia/` is a checkout. Presence of a file is not durability. A file enters
the backup set only when a receipt cites it.

`store` emits `LedgerAction.write`. It does **not** close receipts.
Closers are Curator, `recertia receipt close`, or `upgrade_genesis`.
Sleep is a P5 closer only. `close_receipt` does not charge ADR-0017
`versions_written` (ledger appends are already excluded from that cap).

The ledger is the chain. `StoreReceipt` is the evidence payload of a
`LedgerEntry` with `action="close_receipt"`. Identity is `LedgerEntry.seq` +
`entry_hash`. `receipt_id` is a stable name. Do not grow a second hash-chain
on the receipt (`seq` / `prev_receipt_hash` / `receipt_hash` stay off the P0 model).

`close_receipt` is a T0 publication of citations, not a promotion and not an
`approved` write (ADR-0004, ADR-0005). Sleep / Curator never write `SkillVersion`.
Demotion writes `SkillStatus` plus a new receipt; `version.json` stays immutable
(ADR-0007).

Residue packs (P3) are backup citations. ADR-0018 offload handles are residency.
They are not the same object. Offload stays default-off.

## Phase map

```text
RW-GA soak / live traffic / tabletop-from-tar     unchanged until P4 flip
P0 ADR-0020 + evidence-only StoreReceipt          this PR; Proposed
P1 receipt index in-tree + CLI show/verify        parallel with RW-GA
P2 fold SQLite                                    after P1 green; finishes ADR-0007
P3 residue packs                                  after P2 dual-read
P4 backup --receipts + tabletop-from-receipt      after soak compare (all four predicates)
P5 sleep closer + spectrum + validity             after P4 soak row; HEX still gated
```

Rollback by phase:

- P1–P3: ignore receipts; keep tar-of-tree.
- P4: `--receipts` stays default-off; ADR stays Proposed; go-live sentence stays the tree.
- P5: sleep-closer flag off.

## Cited set

A receipt names four classes. P4 restore copies only these.

| Class | What | When required |
| --- | --- | --- |
| Spine | ledger JSONL (`close_receipt` entries + prior chain) | every receipt |
| Reviewed | `reviewed_git` = HEAD of the product checkout that owns `skills/` (`git rev-parse --show-toplevel`). Not a copy under `.recertia/`. | genesis and every later close |
| Fold | `fold_digest` = hash of a SQLite snapshot of status / stats / cases / edges | P2 onward; null in P1 |
| Residue | L0 transcripts, blobs, workspace snapshots, trajectory JSONL (ADR-0011) | cited by `residue_manifest` (P1) or `residue_cid` (P3) |
| Operator plane | API keys store, soak/eval rows, `job_quota.json`, policy sidecar | **cite as residue** unless a later note marks a file "recreated at restore, not in the unit" |

Coexistence: if `residue_cid` is null, `residue_manifest` is the cite (P1).
If `residue_cid` is set, the cid is the cite and the manifest is advisory or empty (P3).
Never require both.

## Contract shape (lock for P0)

Keep: `receipt_id`, `closed_by` (`curator` \| `sleep` \| `operator` \| `upgrade_genesis`),
`genesis` (bool), `reviewed_git`, `fold_digest`, `residue_cid`, `residue_manifest`,
`compression_moves` (empty valid), `validity_frontier`, `ledger_seq` (pointer back),
`at`.

Drop from the P0 model: `seq`, `prev_receipt_hash`, `receipt_hash`.

## P0 — documents and contract (this PR)

- ADR-0020 Proposed. Does not re-decide ADR-0004 / 0007 / 0009 / 0011 / 0017 / 0018.
- Evidence-only `StoreReceipt` + `LedgerAction.close_receipt` + generated schemas
  (`schema/store_receipt.schema.json`, regenerated `ledger_entry.schema.json`).
- `tests/contracts/test_receipt.py`: empty `compression_moves` valid; `genesis=True`
  valid; extra chain fields rejected.
- RW-SR sibling. This plan. Plans line in `docs/architecture.md`. CHANGELOG Unreleased bullet.
- Do **not** flip go-live.md, tabletop restore, or compile `architecture2.md` sources
  (`remaining-work.md`, operations.md, spec §21). Spec §21 `close_receipt` literal
  waits for the architecture2 fold PR.

**Done when:** `python3 scripts/generate_schemas.py --check` is green; cross-refs pass;
ADR is Proposed; inventory links point at `remaining-work-sr.md`.

## P1 — receipt as an index, layout unchanged

Write home: `recertia.ops.receipts.close_receipt(ledger, store_receipt)` is the **only**
appender. It calls `HashChainLedger.append(action="close_receipt",
evidence=StoreReceipt.model_dump(mode="json"))`. `store`, lift, review, and policy keep
their current actions. Do not scatter `close_receipt` across those call sites.

Derived index: `{runs_root}/receipts.jsonl` (or per-tenant next to `ledger.jsonl`).
`backup_tree` stays `tar.add` of the whole tree — no new `backup_tree` argument in P1.

CLI: `recertia receipt show` / `verify` / `close [--genesis]`.
`--genesis` inventories path+hash of the live tree + `reviewed_git` HEAD + operator-plane
files. Bootstrap does not auto-close.

`reviewed_git` is HEAD of the product checkout that owns `skills/`.
`fold_digest` may be null. `residue_manifest` is the cite.

Verify: P1 `receipt verify` is **advisory** inside the existing tar. A missing genesis
receipt must not fail tabletop-from-tar. `recertia ledger verify` still walks only the
ledger chain (and accepts `close_receipt` evidence that model-validates).

Existing `recertia gc --older-than-days` stays residency GC (snapshots / transcripts /
workspaces). It does not consult receipts. P1 receipts are an index of what was present
at close.

Dual-write stop: if free disk on the runs root drops below 2× current `.recertia/` size,
stop packing new residue (none in P1) and keep tar-of-tree. Alarm is ops, not a metric claim.

**Done when:** `recertia ledger verify` still passes; planted lift goldens unchanged;
tabletop from the current tar still works; a receipt with empty `compression_moves` is
valid; `store` does not emit `close_receipt`.

**Does not:** delete files, change retrieve, enable offload, enable HEX, charge
`versions_written`.

## P2 — fold is SQLite

Move `SkillStatus` event log, `SkillStats` rows, `CaseRecord`, and `edges` into one `.db`.
Dual-read: retrieve and CLI accept SQLite or JSON files. Curator migrator is dry-run first,
then write.

`close_receipt` sets `fold_digest` to the hash of `VACUUM INTO` (or SQLite backup API)
taken at close. Status remains append-only events plus a projection. Stats remain T0 /
rebuildable. JSON dirs are not deleted in this phase.

**Done when:** `recertia cases` / `skills` / metrics report identical answers from SQLite
and from the old files on a fixture library; migrator is reversible; `fold_digest` recomputes.

No new specifications topic. No architecture2 fold required for this phase.

## P3 — residue is a pack

New writes from `TranscriptStore`, `FilesystemBlobStore`, trajectory JSONL (ADR-0011),
and `WorkspaceManager.snapshot` land in a packfile. Readers accept pack *or* loose file.
`snapshot_ref` becomes a path-safe hex CID (`WorkspaceManager.snapshot_path` rejects `/`).
Windows: pack is a file; no reflink required.

`residue_cid` is the cite. Manifest is advisory or empty.

Offline `recertia gc pack` migrates existing loose objects. Do not delete loose files
for citation reasons until P4. Time-based residency GC stays legal.

ADR-0018 stays default-off. Offload handle shape (path, hash, bytes) may match a pack
handle; the objects remain distinct. A CID mismatch on restore is `RoutingError`.

**Done when:** a new run produces no new loose blob/transcript/snapshot/trajectory files
under `.recertia/`; restore of `snapshot_ref` from a pack is hash-identical; `residue_cid`
on the next receipt names that pack.

## P4 — backup copies receipts (durability unit flips)

`backup_tree --receipts` (default **off**) copies the cited set: ledger + `git bundle create
reviewed.bundle HEAD` from the product checkout that owns `skills/` + fold snapshot +
residue packs + operator-plane residue. `recertia tabletop --restore-from` accepts a
receipt directory as well as a tar. Detector + adapter; two-phase restore into a temp dest,
then replace. Failed receipt restore must not leave dest half-written.

Keep the full-tree tar until **one counted soak week** restores both ways. Flip only if
**all four** hold:

1. `ledger verify` green on tar restore and on receipt restore.
2. Planted canary still scores.
3. TTR of both restores written to the soak log.
4. TTR(receipt) ≤ 2× TTR(tar) on that week, or a written waiver.

Fail any → `--receipts` stays default-off, ADR stays Proposed, go-live sentence stays
the tree.

Then, in the same PR as a successful flip:

1. Flip `--receipts` to default.
2. Promote ADR-0020 to **accepted**.
3. Amend operator sentences that name the tree as the unit:
   - [`docs/architecture/go-live.md`](../architecture/go-live.md) backup / RPO row
   - [`docs/architecture/incident-tabletop.md`](../architecture/incident-tabletop.md)
   - backup/restore help text and `backup.py` module docstring
   - remaining-work RW-GA tabletop line (accept receipt dir *or* tar)
   - optional: fold RW-SR into `remaining-work.md` and compile `architecture2.md`

Verify on receipt restore is `RoutingError` (hash mismatch, missing cited object).

Citation-GC: only after the flip. `recertia gc --older-than-days` composed with
"uncited by the last N receipts." **N is policy (default 2).** Do not reuse
`garbage_collect` for citation-GC without that predicate. Time-based residency GC
may continue for objects a receipt does not cite.

**Done when:** tabletop-from-receipt equals tabletop-from-tar on a recorded soak week;
backup artifact count is the spine plus cited objects; `ledger verify` covers receipts;
RPO ≤ 24h; operator plane comes back (keys work on the restored API).

This is the only phase that writes a specifications / go-live durability-unit change.

## P5 — sleep closes the receipt; spectrum and validity

Do not start until P4 has a soak row and RW-HEX predicates are still respected
(`practice_conversion` is a number; `causal_lift` interval exists).

Curator / sleep become receipt closers: pack new residue, snapshot the fold, GC uncited
residue. `compression_moves` only when faithfulness / control-arm evidence already exists.
Sleep never writes `SkillVersion`. Distill → review → store remains the only path into
`reviewed_git`.

Then, separately so it cannot block backup:

- `valid_from` / `valid_to` on fact and case rows; retrieve defaults to current.
- Demotion is a `close_receipt` with `compression_moves.delta = -1`. ADR-0006 evidence
  floor still applies. The git object is superseded, not deleted.
- Split transcripts into tool-turn provenance units *inside* residue. Not a new store.
- SkillVersion commits stay incremental in git. ADR-0017 budget still applies.

`curator_compress` and HEX stay false until their existing predicates fire.

**Done when:** a sleep/Curator pass closes a receipt without a user-facing run; demotion
leaves the git object reachable; retrieve precision@3 is unchanged or reported with a
Wilson interval; HEX flags still false unless RW-HEX predicates hold.

## Success (honesty dialect)

- Tabletop from a receipt succeeds on a counted soak week (P4).
- Lift goldens and the planted canary do not regress. `causal_lift` stays a number or
  `not established` — never a silent zero.
- Faithfulness on compacted cases stays at the current writer threshold.
- `recertia ledger verify` includes receipts.
- Nightly backup after P4 is the receipt spine plus cited objects. RPO ≤ 24h.
- Retrieve precision@3 is unchanged by P1–P4. P5 may change it only with a Wilson interval.
- Do not advertise live-tree inode wins during dual-write (P1–P3).

## Risks

1. Per-run receipts explode the ledger — close on Curator / explicit command / genesis only.
2. Genesis receipt without a full inventory (including operator plane) makes every later restore partial.
3. Dual-write makes disk worse before it gets better. Stop if free disk < 2× tree size.
4. `reviewed_git` is the product checkout that owns `skills/`, not a copied workdir. Drive-letter roots only.
5. Hash mismatch is a hard fail on P4 restore, not a skip (ADR-0018). P1 verify is advisory.
6. Empty `compression_moves` must be legal or P1–P4 wait on lift evidence Recertia does not yet have.
7. Live 14-day GC can delete objects a P1 receipt cited. Citation-GC must not reuse that path until P4.
8. P4 flip that omits API keys / soak / `job_quota` drops operator-GA state.

## Non-goals

Neo4j, Graphiti, A-MEM neighbor rewrite, KV persistence, weight updates, ReFS requirement,
retrieve ranking rewrite, enabling ADR-0018 offload, enabling HEX/compress, a sixteenth
graph node, marking `a1`/`a2`/`a4` `supported` from CI, a second receipt hash-chain,
folding `remaining-work.md` + `architecture2.md` in P0–P3.
