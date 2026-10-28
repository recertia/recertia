# ADR-0022: REL-3 R4 — an open that finds the root lock held is typed `RootBusy`, never a leftover overlay

- **Status:** accepted
- **Date:** 2026-10-22
- **Accepted:** 2026-10-22, accepted by the owner
- **Decides:** [#138](https://github.com/recertia/recertia/issues/138) (REC-056h). It re-scopes and unblocks [#139](https://github.com/recertia/recertia/issues/139) (REC-056b2).
- **Related:** [ADR-0003](0003-criteria-preregistration.md), [ADR-0020](0020-store-receipts.md) (backup/restore unit), [ADR-0021](0021-deployment-model.md) (D2: local execution only in `single_node`, behind the break-glass)
- **Plan:** REC-056 re-slice of closed [#91](https://github.com/recertia/recertia/pull/91) (rows REC-056h and REC-056b2)
- **Evidence:** Independent verification of the accepted text passed, and an independent spot-check of the amended text passed with no blocking findings.
- **Reviewed assertion text:** the b2 prototype patch, sha256 `84886788a0a9084c0cd2438ee41f970ec261b506fe62fc921ad86085a8ce695b`, a test-only `git diff` of four files against main `49619a6cf0436c11c5c784f6b97a4e1e1777f2d8`. It lands as the test commit(s) of the #139 PR.
- **Follow-ups:** [#144](https://github.com/recertia/recertia/issues/144) (REC-058), [#145](https://github.com/recertia/recertia/issues/145) (REC-059), [#146](https://github.com/recertia/recertia/issues/146) (REC-060), [#147](https://github.com/recertia/recertia/issues/147) (REC-061), plus one proposed new item (Follow-ups, item 5)

**Amendment (2026-10-22).** After acceptance, the done-when item 4 guard was hardened per independent verification. That verification reported one blocker: a new `tests/unit/graph/conftest.py` whose `pytest_runtest_makereport` hookwrapper turns failed reports into passes, combined with a `src/` mutation, kept the item 4 guard at exit 0 and the 6 / 12 node IDs green. The amendment adds done-when item 10 (the PR's file set must equal an exact six-path allowlist), which closes that blocker, and items 11–13 (the guard rejects any `RESULTS` mutation in the busy block, pins the added definitions by full sha256, and pins every rewritten or added body by full sha256). It also corrects the b1 PR number to [#151](https://github.com/recertia/recertia/pull/151). The decision itself and the reviewed assertion text are unchanged.

**Where the cites point.**
- Every line cite is at main `49619a6cf0436c11c5c784f6b97a4e1e1777f2d8` unless stated. Main is REC-051 `e25ecfa6`, then REC-052 (`f5ab94b8`, `87bbc1ae`), then REC-056b1 as [#151](https://github.com/recertia/recertia/pull/151) (merge `49619a6c`). [#148](https://github.com/recertia/recertia/pull/148), with the same code as #151, is still open and superseded; it did not land.
- What moved since earlier cites at `e25ecfa6` / #148 head `95d29e48`:
  - #151: `tests/unit/persistence/test_import_legacy.py` gains 12 lines, so `:207-231` / `:482` / `:508` are now `:219-243` / `:494` / `:520`; `root_lock.py` loses the stub at `:36-43`, so `:116` is now `:106`.
  - REC-052: `tests/probes/rel3_probe.py` `:311` is now `:397`; `rel3_probe_busy.py` `:14` / `:18` are now `:13` / `:17`; `rel3_probe_r3.py` `:162-163` is now `:165-166`.
  - `CHANGELOG.md`: the REL-3 runtime.db entry (`:239-257` at `e25ecfa6`) is now `:268-286`.

## Context

**What main does today.**

`open_run_persistence` (`src/recertia/graph/persistence.py:99-173`) calls `import_legacy_runtime` when `RECERTIA_RUNTIME_DB_MIGRATE=auto` (`:198`). If leftover `checkpoints.db` / `operations.db` are still pending:
- the importer waits up to `RECERTIA_IMPORT_LOCK_TIMEOUT_S` for the exclusive root lock (`src/recertia/persistence/import_legacy.py:170-175`, default 5 s; the test suite sets 0.25 s at `tests/conftest.py:18`);
- if the lock is still held, it raises typed `RootBusy` and writes nothing (`import_legacy.py:191-201`; `RootBusy` is defined at `src/recertia/persistence/root_lock.py:28-33`).

**Amended by REC-065 ([#170](https://github.com/recertia/recertia/pull/170)).** The importer now holds root shared + tenant exclusive, and "the lock is held" now includes a same-tenant tenant lock. The decision (typed `RootBusy`, fail closed, no overlay) is unchanged, and so is the rule's scope (the leftover-import path); a different tenant's handle or import no longer makes it busy (#158).

The lock is a flock and must not carry pid/tid identity (`root_lock.py:6-8`). The old identity helper was a stub that always returned `False` (`root_lock.py:36-43` at `e25ecfa6`); REC-056b1 ([#132](https://github.com/recertia/recertia/issues/132)) deleted it, and that landed on main as [#151](https://github.com/recertia/recertia/pull/151).

The shipped contract says the same thing:
- `CHANGELOG.md:268-286` (the quoted sentence is `:273-275`): "Anyone who finds exclusive `.lock` already held waits, then raises typed `RootBusy` … no same-thread holder exception; `.lock` stores no pid/tid identity".
- The `open_run_persistence` docstring at `graph/persistence.py:112-114`.
- The unit tests `tests/unit/persistence/test_import_legacy.py:219-243` (`test_same_thread_holder_gets_root_busy`) and `:494` (`test_busy_open_raises_root_busy_without_mutating_sqlite`).

**The conflict.** Two families of REL-3 probes encode opposite rules for the same moment (an open while another holder has the exclusive root lock):

1. **The R3 probes (green on main).** `tests/probes/rel3_probe_r3.py:62-65`: "open under exclusive RootLock must either raise a typed *Busy* error within BUSY_T, or block and succeed only after release. Nothing may be written during the busy window".
   - An open that succeeds while the lock is held is recorded as a failure: "open SUCCEEDED while lock held -- busy window not honoured" (`rel3_probe_r3.py:165-166`).
   - These are the 12 node IDs O/M3/M4 a–d at `tests/unit/graph/test_rel3_probe.py:288-409`.
   - T3/T3b/T4/T5 (`test_rel3_probe.py:506-531`; `tests/probes/rel3_probe_r4.py:44-102`) also require that no lock identity exists. T4/T5 additionally require that an unrelated opener on the holder's own thread (an asyncio coroutine, or a pool worker) gets `RootBusy`.
2. **The R2/R4 expectations (skipped on main).** Six probes expect an open under the held lock to succeed and overlay the leftover files:
   - I: `tests/probes/rel3_probe.py:397`, "lock busy -> falls back to legacy files".
   - L, M, M2: `tests/probes/rel3_probe_busy.py:13`, "busy -> runtime.db, no import", then a busy-path write at `:17`.
   - T1, T2: `rel3_probe_r4.py:26-42`, a "same-thread holder" exception.
   - They are skipped unconditionally at `test_rel3_probe.py:122, 184, 198, 212, 480, 493`. The skip reasons already name the conflict: "R3 O/M3/M4 require typed RootBusy or block-until-release" and "86c9969 dropped pid/tid identity so a busy open is always RootBusy (T4/T5)".

**Failure mode on main (re-run at `49619a6c`).** Removing only the six skips and running the six node IDs gives **6 failed**.
- **I:** `scen_import_split_brain raised RootBusy: cannot import leftover runtime files while the root lock is held` (raised at `rel3_probe.py:397`, turned into a failure by `test_rel3_probe.py:43-48`).
- **L, M, M2:** the probe module dies at `rel3_probe_busy.py:13` with `RootBusy`. `_busy_records` (`test_rel3_probe.py:144-174`) then records all three as "probe error RootBusy". M2 fails only because the shared module never reaches it.
- **T1, T2:** `open_run_persistence` raises `RootBusy` at `rel3_probe_r4.py:30` / `:38`. `scen` records the function name instead of the probe name, so the test reports "probe did not record 'T1 …' / 'T2 …'".

**Why the six cannot simply be made to pass as written.** T1/T2 need an open on the holder's own thread to succeed, while T4/T5 need an open on the holder's own thread to fail. With no lock identity (T3/T3b), the code cannot tell the two cases apart. I and L need an open under the held lock to succeed, which O/M3/M4-a forbid. Satisfying the six as written requires editing the 12 O/M3/M4 assertions and T3–T5, and reintroducing identity.

**Security setting.**
- The repo is public, so these probes and this record are public. Nothing here depends on secrecy.
- In `single_node`, caller-supplied criteria commands may run on the local backend behind the break-glass (ADR-0021 D2), as the same OS user that owns the runs root.
- `.lock` is created `0o600` (`root_lock.py:106`), so any process running as that user can take the flock or write text into the file. That includes an untrusted criterion running locally.

## Options

### A. Leftover overlay while the lock is held (what the six probes encode)

The busy open succeeds. It reads from the leftover files plus `runtime.db` (`CheckpointStore(read_fallback=…)`; `graph/persistence.py:149-152`, with the now-dead `read_fallback` return at `:201-202`), writes to `runtime.db`, and the later import merges.

- **Concurrency:** two sources of truth for one run during the busy window.
  - The merge has to resolve conflicts after the fact. The current rule keeps the destination row and drops the leftover row at the same seq (`import_legacy.py:375-380`, "Busy-path ghost writes keep their seq"), so the busy write silently overwrites history. That is exactly what M4 and T2 exist to catch.
  - An op that already ran in the leftover files can run again in the overlay (T1's double run). Avoiding that needs cross-file op dedupe while another process is mid-import.
- **Backup / restore:** the holder of the exclusive lock is the importer or backup/restore (`src/recertia/ops/backup.py:179-197`). An overlay lets writers mutate the root during a backup tar or a restore, so a backup can capture a torn tree and a restore can be mixed with live writes. That undermines the restore unit (ADR-0020) and the hash-chained ledger that ADR-0003's pre-registration relies on.
- **Same-thread exception (T1/T2):** needs holder identity, which T3/T3b forbid. Identity text in a user-writable `0o600` file can be forged, by a SIGKILLed holder's leftover text, PID 1 reuse in containers, or a local criterion process writing the file (`rel3_probe_r4.py:46-48`).
- **Back-compat:** reverses a shipped contract (`CHANGELOG.md:268-286`) and the two unit tests above.
- **Test impact:** I/L/M/M2/T1/T2 pass only if O/M3/M4-a and -b (6 of the 12 node IDs), T3/T3b/T4/T5 and the two `test_import_legacy.py` tests are rewritten. **This fails the binding condition that the 12 O/M3/M4 node IDs stay green in the overlay branch.**

### B. Typed `RootBusy`, bounded wait, nothing written — keep main, rewrite the six probes by design (recommended)

An open that needs the leftover import and finds the exclusive lock held waits at most `RECERTIA_IMPORT_LOCK_TIMEOUT_S`, then raises `RootBusy` and writes nothing. The caller retries after release. The six probes are rewritten to assert this behaviour plus convergence after release. The assertions that guard data stay; only the "succeeds while busy" expectations change.

- **Concurrency:** one writer path per root during import, so no merge and no ghost writes. After release, the first open imports exactly once, and later opens converge (M, M2, O-d, M3-d, M4-d).
- **Security:** fail-closed.
  - No identity surface, so nothing to forge.
  - A hostile local process that holds the flock can only cause typed, bounded `RootBusy` (availability), never split-brain or lost writes (integrity).
  - Backup/restore see a quiescent root on the import path.
- **Availability cost:** while backup or an import holds the lock, opens that need the import fail after ≤ 5 s by default. Callers must treat this as retryable. Today `RootBusy` is a `RuntimeError` subclass (`root_lock.py:24-33`), not the `RecertiaError` with code `root_busy`, 503, retryable, exit 6 that the spec names (`docs/plans/remediation/rel-specs.md:297`). See Consequences.
- **Back-compat:** no product change. This is the shipped behaviour.
- **Test impact:** test-only. The prototype shows the six pass with no `src/` change while the 12 O/M3/M4 IDs stay green.

### C. Block until release (unbounded wait)

R3 also allows mode (b), "block and succeed only after release" (`rel3_probe_r3.py:62-64`).

- **Concurrency:** correct after release.
- **Availability and security:** a single holder (a slow backup, or a hostile local criterion holding the flock) hangs every open indefinitely. API sync workers pile up behind it, which is a denial of service on a public service.
- **Deadlock:** I and L hold the lock in a child process that is released later by the same script. T1/T2 hold it on the opener's own thread. All four would deadlock rather than fail, so they would still need rewriting.
- **Verdict:** strictly worse than B. B's bounded wait already gives the "wait a little" benefit.

## Decision

**B.**
- On the leftover-import path, an open that finds the root's exclusive lock held raises typed `RootBusy` within the configured bound and writes nothing.
- There is no overlay, no read-fallback merge, and no same-thread or holder exception.
- The lock carries no identity.
- After release, the first open imports exactly once, and every later open converges on exactly the pre-existing state plus the holder's own writes.

**Amended by REC-065 ([#170](https://github.com/recertia/recertia/pull/170)).** The importer now holds root shared + tenant exclusive, and "the lock is held" now includes a same-tenant tenant lock. The decision (typed `RootBusy`, fail closed, no overlay) is unchanged, and so is the rule's scope (the leftover-import path); a different tenant's handle or import no longer makes it busy (#158).

The six probes I, L, M, M2, T1, T2 are **rewritten by design** to the expectations in the next section. Their node IDs do not change.

## Per-probe expected results (binding for #139)

`BUSY_BOUND_S` = 10 s: the import wait (0.25 s under `tests/conftest.py:18`, 5 s by default) plus slack.

"Busy open OK" means **all** of these hold:
- `open_run_persistence(root)` raises `RootBusy`, exactly that type or a subclass. A generic error or a success is a failure.
- It does so within `BUSY_BOUND_S`.
- No root-level file whose name contains `db` changes or appears during the call (the same notion as `rel3_probe_r3.py:86-91`).

The patch adds this check to `tests/probes/rel3_probe.py` as `BUSY_BOUND_S`, `db_snapshot(root)` (sha256 of every root-level file with `db` in its name) and `busy_open(root)`, which returns `(ok, detail)` with `ok` true only when all three conditions hold.

| Probe (node ID in `tests/unit/graph/test_rel3_probe.py`) | Recorded name | New expected result | Changed? Why |
|---|---|---|---|
| **I** `test_probe_i_no_state_lost_across_concurrent_import` | `I no state lost across concurrent import` (unchanged) | Leftover run `old-run` exists. A child process holds the exclusive lock. **Busy open OK** for the would-be writer p2. After release: p1 opens and imports; p2 **retries** the open and writes `p2-run`; a fresh p3 lists **exactly** `["old-run", "p2-run"]`. | **Changed.** The busy open is refused instead of "falling back to legacy files" (old `rel3_probe.py:397`). The "no state lost" assertion is kept and tightened from "contains both" to equality, plus the typed-busy and no-write checks. |
| **L** `test_probe_l_legacy_visible_while_lock_busy` | `L busy open is typed RootBusy, writes nothing; legacy state intact` (renamed) | Leftover seqs 1–3 for `legacy-run`. A child process holds the exclusive lock. **Busy open OK**, and the leftover `checkpoints.db` (read-only) still holds exactly `[(1,"n1"),(2,"n2"),(3,"n3")]`. | **Changed.** The old expectation "legacy state visible to the busy-path process (`latest_seq == 3`)" is dropped by design, because under B there is no busy-path process. Visibility moves to after release (M). |
| **M** `test_probe_m_import_after_busy_keeps_state` | `M import after the refused busy open keeps all legacy state exactly` (renamed) | Same root after L's refused open and the release. The first normal open imports, and `history("legacy-run")` is **exactly** `[(1,"n1"),(2,"n2"),(3,"n3")]`. | **Changed.** The old scenario's busy-path write at seq 1 (`rel3_probe_busy.py:17`) cannot happen under B, so M asserts exact preservation (no loss, no extra seq) instead of `latest_seq == 3` after a conflicting write. |
| **M2** `test_probe_m2_root_openable_after_conflict` | `M2 root still openable after conflict` (unchanged) | A second fresh open after M succeeds (`latest_seq == 3`). | **Unchanged assertion.** It tests recoverability, which B preserves. On main it fails only because the shared probe module dies earlier at `rel3_probe_busy.py:13`. The harness fix (L/M no longer raise) is the only change that reaches it. |
| **T1** `test_probe_t1_same_thread_ops_not_rerun` | `T1 same-thread holder: op done in legacy not re-executed` (unchanged) | Leftover op `("legacy-run",1,"solve",0)` already executed once (effect `legacy`). The **same thread** holds the exclusive lock through a separate `RootLock`. **Busy open OK** (no holder exception, consistent with T4/T5 and `test_import_legacy.py:219-243`). After release, open and `run_once` the same op. It returns the imported result **without executing**, so the effects stay exactly `["legacy"]`. | **Changed** mechanism (the open is refused instead of succeeding as "holder"). The "not re-executed" assertion is kept and now also covers the post-release open. |
| **T2** `test_probe_t2_same_thread_ghost_write_rejected` | `T2 same-thread holder: write at a seq that exists in leftover is rejected, nothing lost` (unchanged) | Leftover seqs 1–3. The same thread holds the exclusive lock. **Busy open OK**: the ghost write is rejected because the open itself is refused. After release and import, the history is **exactly** `[(1,"n1"),(2,"n2"),(3,"n3")]`. | **Changed** mechanism. "Rejected, nothing lost" is kept and tightened from "any non-ACCEPTED outcome" to "typed `RootBusy`, no db write". |

**Not changed** (identical to main `49619a6c`; done-when item 4): `tests/probes/rel3_probe_r3.py` (O/M3/M4/P/Q/R/S), `tests/unit/persistence/test_import_legacy.py`, T3/T3b/T4/T5 in `rel3_probe_r4.py` and their wrappers (decorators included), every other function and the module-level code of `tests/probes/rel3_probe.py` (including `record`), and the setup, lock, release and M2 statements of `rel3_probe_busy.py`. The REC-056b1 edit to T3 is separate and landed as #151.

## #139 (REC-056b2) done-when, re-scoped to this decision

b2 is a **six-file PR**: four test files (the reviewed patch) plus the ADR doc `docs/adr/0022-rootbusy-no-leftover-overlay.md` and `CHANGELOG.md` (item 10), and still **no `src/` change**. It applies the reviewed prototype patch (sha256 `84886788a0a9084c0cd2438ee41f970ec261b506fe62fc921ad86085a8ce695b`), which removes the six `pytest.mark.skip(…)` decorators and rewrites I/L/M/T1/T2 as in the table. M2 changes only through the shared module.

| File | Change |
|---|---|
| `tests/probes/rel3_probe.py` | adds `BUSY_BOUND_S`, `db_snapshot`, `busy_open`; rewrites `scen_import_split_brain` (I) |
| `tests/probes/rel3_probe_busy.py` | L and M rewritten per the table; M2 unchanged |
| `tests/probes/rel3_probe_r4.py` | T1 and T2 rewritten; imports `busy_open`; T3–T5 untouched |
| `tests/unit/graph/test_rel3_probe.py` | removes the six `pytest.mark.skip(…)` blocks; L/M names updated in `_busy_records` and the two wrappers; module docstring gains the ADR-0022 exception (CRLF kept) |

**Base and order.** b1 (REC-056b1) landed on main as [#151](https://github.com/recertia/recertia/pull/151) (merge `49619a6c`), and b2 is based on it. Every "byte-identical" / "no diff" check below is against **origin/main `49619a6c`** (or later main, re-checked).

The patch was first built on #148 head `95d29e48` (on `e25ecfa6`). Main then took REC-052 (`f5ab94b8`, `87bbc1ae`, a probe temp-dir cleanup), which touches `CHANGELOG.md` and the probe files but not the asserted behaviour. Rebasing onto `49619a6c`:
- `git apply -3` applies three files cleanly (`rel3_probe.py` at an offset of 86–87 lines);
- `rel3_probe_busy.py` has one known one-line import conflict. Resolution: `from tests.probes.rel3_probe import state, record, RESULTS, owned_root, busy_open`;
- there is no other difference: the rebased patch equals the original apart from `index`/hunk-offset lines and that import line.

Any other rebase difference goes back to review (item 8).

1. **The six pass:** 6 passed, 0 skipped, 0 xfail, each recorded name exactly as in the table:
   ```
   pytest \
     tests/unit/graph/test_rel3_probe.py::test_probe_i_no_state_lost_across_concurrent_import \
     tests/unit/graph/test_rel3_probe.py::test_probe_l_legacy_visible_while_lock_busy \
     tests/unit/graph/test_rel3_probe.py::test_probe_m_import_after_busy_keeps_state \
     tests/unit/graph/test_rel3_probe.py::test_probe_m2_root_openable_after_conflict \
     tests/unit/graph/test_rel3_probe.py::test_probe_t1_same_thread_ops_not_rerun \
     tests/unit/graph/test_rel3_probe.py::test_probe_t2_same_thread_ghost_write_rejected
   ```
2. **The 12 O/M3/M4 stay green:** 12 passed. The functions are at main `test_rel3_probe.py:288-409`.
   ```
   pytest \
     tests/unit/graph/test_rel3_probe.py::test_probe_o_a_typed_busy_or_block \
     tests/unit/graph/test_rel3_probe.py::test_probe_o_b_nothing_written_during_busy \
     tests/unit/graph/test_rel3_probe.py::test_probe_o_c_no_side_effect_or_double_run \
     tests/unit/graph/test_rel3_probe.py::test_probe_o_d_after_release_exact_history \
     tests/unit/graph/test_rel3_probe.py::test_probe_m3_a_typed_busy_or_block \
     tests/unit/graph/test_rel3_probe.py::test_probe_m3_b_nothing_written_during_busy \
     tests/unit/graph/test_rel3_probe.py::test_probe_m3_c_no_side_effect_or_double_run \
     tests/unit/graph/test_rel3_probe.py::test_probe_m3_d_after_release_exact_history \
     tests/unit/graph/test_rel3_probe.py::test_probe_m4_a_typed_busy_or_block \
     tests/unit/graph/test_rel3_probe.py::test_probe_m4_b_nothing_written_during_busy \
     tests/unit/graph/test_rel3_probe.py::test_probe_m4_c_no_side_effect_or_double_run \
     tests/unit/graph/test_rel3_probe.py::test_probe_m4_d_after_release_exact_history
   ```
   **The same requirement would hold in the overlay branch.** That is why A is rejected: A cannot meet it without editing O/M3/M4-a/-b.
3. **`test_import_legacy.py` is unchanged and passing.**
   - `pytest tests/unit/persistence/test_import_legacy.py` gives 23 passed at `49619a6c`, with the rebased patch applied.
   - This file is the only guard against an import that ignores the configured busy timeout (mutation F below). The `< 1.5 s` bound is at `test_import_legacy.py:520`. The six probes and the 12 allow up to `BUSY_BOUND_S` = 10 s and `BUSY_T` = 5 s, so they do not catch it.
4. **Identity guards (N3, hardened G1/G2).** Against origin/main `49619a6c`:
   - `tests/unit/persistence/test_import_legacy.py` and `tests/probes/rel3_probe_r3.py` are byte-identical;
   - in `tests/probes/rel3_probe.py`, `tests/probes/rel3_probe_busy.py`, `tests/probes/rel3_probe_r4.py` and `tests/unit/graph/test_rel3_probe.py`, every top-level function is source-identical **including its decorators** (G1), except the bodies the patch rewrites: `scen_import_split_brain` (I), `T1_same_thread_ops_double_run`, `T2_same_thread_ghost_write`, `_busy_records`, `test_probe_l_legacy_visible_while_lock_busy` and `test_probe_m_import_after_busy_keeps_state`. `subprocess_exclusive` may change only its docstring. That covers `record`, `state`, `fresh_root`, `owned_root`, T3, `_complete`, T3b, T4, T5, the helpers, the T3/T3b/T4/T5 wrappers and the 12 O/M3/M4 wrappers;
   - decorators are checked on the rewritten functions too. The six un-skipped wrappers may lose exactly their one unconditional `pytest.mark.skip(…)` and nothing else; every other decorator list must be unchanged, so adding an xfail or skip decorator anywhere fails;
   - module-level code in all four files is unchanged (G2), except: the module docstrings of `rel3_probe_busy.py`, `rel3_probe_r4.py` and `test_rel3_probe.py`; `busy_open` appended to the `from tests.probes.rel3_probe import …` line of `rel3_probe_busy.py` and `rel3_probe_r4.py`; and, in `rel3_probe_busy.py`'s `with owned_root() as root:` block, the L/M statements. In that block the header and the setup, lock, release and M2 statements stay identical and in order, and no new statement may rebind `record`, `RESULTS`, `state`, `busy_open`, `owned_root`, `open_run_persistence`, `RootLock` or `RootBusy`, assign an attribute, or use `global`/`nonlocal`;
   - the three definitions the patch adds to `rel3_probe.py` (`BUSY_BOUND_S`, `db_snapshot`, `busy_open`) are pinned to the reviewed text by sha256 (item 12); no other top-level definition may be added or removed.

   The guard is a Python AST comparison of the four files against the base commit (see "Guard" below). On the rebased prototype it reports 95 functions `same` (6 of them with the body rewritten by design, decorators checked), the `rel3_probe_busy.py` block `same` outside L/M, 3 additions `pinned`, both files `identical`, 14 item 13 pins matching, and `OK: 0 difference(s)`. The mutation table under Prototype shows what it catches.
5. **No skips:** `grep -c "pytest.mark.skip(" tests/unit/graph/test_rel3_probe.py` returns 0 (main has 6), and `pytest tests/unit/graph/test_rel3_probe.py` reports 0 skipped. The conditional `skipif` decorators stay.
6. **No `src/` diff:** `git diff origin/main -- src/` is empty. Any `src/` change in b2 needs a maintainer decision first.
7. **Gates:**
   - the `ruff check contracts/ src/ scripts/ tests/ conftest.py` CI step is clean;
   - `python3 scripts/mypy_ratchet.py` holds at 197 errors;
   - the full suite is green (rebased prototype on `49619a6c`: 1804 passed, 3 skipped, 1 xfailed);
   - `ci` is green at the PR head;
   - line endings are unchanged (`git ls-files --eol`): `test_rel3_probe.py`, `rel3_probe_r3.py` and `test_import_legacy.py` stay CRLF, and the three other probe files stay LF.
8. **Review before b2 starts.** The reviewed patch is the assertion text. The implementer applies it as-is, apart from the rebase resolution named above. Any other deviation goes to review before code is written, and the final six-file PR diff (the four-file test assertion diff plus the ADR doc and `CHANGELOG.md`; no `src/`) is signed off before the PR is opened.
9. **Mutations** (verification): each of A–F must turn exactly the tests listed under Prototype red, with F red only in `test_import_legacy.py`.
10. **File-set allowlist (closes the conftest hookwrapper blocker).** In the b2 PR, against origin/main (`49619a6c` or later main):
    - **(a) Exact file set.** `git diff --name-only origin/main...HEAD` equals **exactly** these six paths, nothing more and nothing less:
      - the four files the patch touches: `tests/probes/rel3_probe.py`, `tests/probes/rel3_probe_busy.py`, `tests/probes/rel3_probe_r4.py`, `tests/unit/graph/test_rel3_probe.py`;
      - the two docs paths: `docs/adr/0022-rootbusy-no-leftover-overlay.md` (this record; if it is renumbered or renamed, this exact path changes by amendment, never by glob) and `CHANGELOG.md`.
    - **(b) Commit split.** The two docs paths are in their own commit, and that commit touches only those two paths. The patch commit(s) touch only the four patch files. Exactly one docs commit; no merge commits in `origin/main..HEAD`.
    - **(c) Docs are inert to pytest.** Neither docs path is a pytest config or a collected file: both are `.md`, outside `testpaths = ["tests"]`, and `pyproject.toml` (which carries `[tool.pytest.ini_options]`) is not in the set.
    - This shuts out any new or edited `conftest.py` (at any level), `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini` or other pytest config or plugin, and any `src/` change, whatever its content. Item 4's guard only inspects the four files, so this item is what closes the rest of the tree. The check (see "File-set check" below) must exit 0.
    - **Item 10 is not sufficient on its own; the guard run is mandatory.** A spot-check showed that a hook placed in an allowlisted file (`tests/probes/rel3_probe.py` plus `pytest_plugins`) gets past item 10 and hides mutation B; only the guard catches it. The guard (items 4 and 11–13) must be run on the b2 PR head and exit 0, in addition to the file-set check.
11. **Guard: no `RESULTS` mutation in the busy block** (checked in the b2 review). The item 4 guard as run on the b2 PR must reject any statement added inside `rel3_probe_busy.py`'s `with owned_root() as root:` block that mutates `RESULTS` beyond the reviewed append form. The only reviewed form is a `record(name, ok, detail)` call, which appends inside the pinned `record()`; the reviewed L/M text never names `RESULTS`. So a new with-block statement that references `RESULTS` at all fails, including slice assignment (`RESULTS[:] = …`), item assignment, `RESULTS.__setitem__(…)`, `.extend` / `.insert` / `.pop` / `.remove` / `.clear` / `.sort`, `del RESULTS[…]`, augmented assignment (`RESULTS += …`), rebinding and aliasing. Indirect reach (`__import__(…).RESULTS`, `getattr`/`setattr`, `globals()`, `vars()`, `sys.modules`) in a new statement also fails. The guard as accepted (before this amendment) did **not** meet this: a `RESULTS[:] = …` or `RESULTS.__setitem__(…)` line after the L `record` got `OK: 0 difference(s)` and flipped L from fail to pass (proof under Prototype).
12. **Guard: full sha256 pins** (checked in the b2 review). The guard pins the three added definitions, `BUSY_BOUND_S` included, by their full 64-hex sha256, not a prefix (the guard as accepted truncated to 16 hex; the guard now compares the full digest for all three). The full values, computed on the rebased prototype with the same LF source-segment rule as item 13:
    - `BUSY_BOUND_S`: `082e5ba9669d4957e3cccec1b19da8cf7713de92ccc99716ff813fd8f28b5273`
    - `db_snapshot`: `e7014e6f699a86558811581e7d2c3e43bfa0179acd6699ed8e9bf53019b2fca4`
    - `busy_open`: `19544f62f5db2406fb87ed126edb482f10fb2d5b843d67068dc45059cffe089f`
13. **Guard: full sha256 pins of every rewritten or added body** (checked in the b2 review). The guard also pins, by full 64-hex sha256, each top-level function the patch rewrites or adds, and the one module-level block it rewrites. That closes the gap that the rewritten bodies were otherwise unchecked, so any edit to them fails, including a `RESULTS` edit. Normalization is the one the guard uses for the byte-identical check of unrewritten functions: the `ast.get_source_segment` of the top-level node on the CRLF→LF-normalized source, UTF-8 encoded. That runs from `def` (or `with`) to the last body line, with decorators excluded, since G1 checks them. Each pin equals that text in patch `84886788a0a9084c0cd2438ee41f970ec261b506fe62fc921ad86085a8ce695b` applied to `49619a6c`. The list is complete: it was derived by comparing every top-level def and `with` block of the four files between `49619a6c` and the patched tree, giving 13 functions plus the busy block. Four of the 13 (`test_probe_i_no_state_lost_across_concurrent_import`, `test_probe_m2_root_openable_after_conflict`, `test_probe_t1_same_thread_ops_not_rerun`, `test_probe_t2_same_thread_ghost_write_rejected`) only lost their `pytest.mark.skip` decorator: their pinned text is identical to `49619a6c`, and the pin locks it against later edits.
    - `tests/probes/rel3_probe.py`:
      - `subprocess_exclusive` (docstring only): `1af9ac64bff6bff250a7c6f3c11fd6acf5a6486dc776c7e85cec6aba08184b75`
      - `db_snapshot` (added): `e7014e6f699a86558811581e7d2c3e43bfa0179acd6699ed8e9bf53019b2fca4`
      - `busy_open` (added): `19544f62f5db2406fb87ed126edb482f10fb2d5b843d67068dc45059cffe089f`
      - `scen_import_split_brain`: `1ab235844ccdf59bbff6050f958e15230088c507aff38fe723dc8cd4c9f05163`
    - `tests/probes/rel3_probe_busy.py`: the `with owned_root() as root:` block (L/M rewritten): `f575be7a6f79276e33818d1d263ae139f44d74ee9455800d4b843e8886c33284`
    - `tests/probes/rel3_probe_r4.py`:
      - `T1_same_thread_ops_double_run`: `8f2e5a3722dc10a33e312c365cd115b3ab4211c6f080e518b1b65912668c3287`
      - `T2_same_thread_ghost_write`: `a8e2481a931aad78245c846e81bb20f0640e534610911ebcdb9fb8d6172bcbe1`
    - `tests/unit/graph/test_rel3_probe.py`:
      - `_busy_records`: `a4608c900d1c59eaa317daf2b6464897d5157f27f71fe8d147a6b5a99a6beaa4`
      - `test_probe_i_no_state_lost_across_concurrent_import` (skip removed): `7b6390b288b3df34fe6015c270625a7c30969935ffe6c056b41376c39ca8281e`
      - `test_probe_l_legacy_visible_while_lock_busy` (skip removed, body rewritten): `c86bf14614492eb5b461df626bdc49aa09a8224c355cfc0c0fba7b5936d363fe`
      - `test_probe_m_import_after_busy_keeps_state` (skip removed, body rewritten): `b5d2803dc1ceed8e939938ba761cf8558d7bded5b6145986d018259881742044`
      - `test_probe_m2_root_openable_after_conflict` (skip removed): `2c4a6145fb203deb26cf8e4d2a85f4a4221b99fa728a57ea2be0f4e423b590f8`
      - `test_probe_t1_same_thread_ops_not_rerun` (skip removed): `fd92cc9438b7d241f9646b8fc6ed8a4541b5a1d8e190b9715987d001bc45c21d`
      - `test_probe_t2_same_thread_ghost_write_rejected` (skip removed): `e0b8d0faa66bda4719c776ab86c115c29ed6b141fffce8a64f882fc831103d1d`
    - `db_snapshot` and `busy_open` are also pinned by item 12 (same full values). `BUSY_BOUND_S` is a constant, not a function, and stays under item 12. The module docstrings and the `busy_open` import additions stay under G2.

Items 10–13 are binding. Items 11–13 are requirements on the guard run in the b2 review. Running the guard (exit 0) and the file-set check (exit 0) on the b2 PR head are both mandatory; neither replaces the other. The reviewed patch is unchanged by the amendment and stays the reference assertion text.

### Guard and file-set check (run in review, not committed)

Both checks are mandatory: they are run in the b2 review and in verification, from the repo root of the b2 branch, and each must exit 0. Neither is added to the repo: item 10 allows exactly six paths.

**Guard** (done-when items 4 and 11–13), invoked with the base commit:
- parses the four patch files at the base (`git show <base>:<path>`) and in the working tree, both CRLF→LF-normalized, with `ast`;
- for every top-level function or class: requires the same set of names (plus only the pinned additions), the same decorator list (minus exactly one unconditional `pytest.mark.skip` on the six un-skipped wrappers), and a byte-identical source segment unless the function is on the rewritten list (or, for `subprocess_exclusive`, identical once the docstring is dropped);
- for module-level statements: requires the same count and order, with only the listed exceptions (module docstring, the `busy_open` import addition, the L/M statements of the busy `with` block), and rejects a new busy-block statement that rebinds a pinned name, assigns an attribute, uses `global`/`nonlocal`, or references `RESULTS` directly or indirectly (item 11);
- checks the three added definitions (`BUSY_BOUND_S`, `db_snapshot`, `busy_open`) and the 14 item 13 targets against their full 64-hex sha256 pins;
- runs `git diff --quiet <base> -- <path>` for `test_import_legacy.py` and `rel3_probe_r3.py`;
- prints one line per check and `OK: 0 difference(s)` with exit 0, or `FAIL: n difference(s)` with exit 1.

**File-set check** (done-when item 10), invoked with the base commit:
- runs with `LC_ALL=C` exported, so the `sort` and `comm` comparisons below do not depend on the locale;
- (a) compares the sorted `git diff --name-only <base>...HEAD` with the six allowlisted paths and prints any extra or missing path;
- (b) walks `git rev-list --reverse <base>..HEAD`, fails on any merge commit, classifies each commit as a docs commit (touches exactly the two docs paths) or a patch commit (touches only the four patch files), fails any other commit, and requires exactly one docs commit;
- (c) fails if either docs path is a `conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini` or `*.py`, or sits under `tests/`;
- prints `FILESET OK` with exit 0, or `FILESET FAIL` with exit 1.

## Consequences

- **No product change.** REL-3 R4 closes with main's behaviour, the CHANGELOG wording and the `open_run_persistence` docstring all as they are. The module docstring at `test_rel3_probe.py:1-5` ("Probe assertions are not edited") gains the ADR-0022 exception, which is in the patch.
- **Dead overlay plumbing.** `_resolve_runtime_db`'s `read_fallback` return (`graph/persistence.py:201-202`) is unreachable, because `import_legacy_runtime` (called at the live `:198`) returns `True` or raises. `CheckpointStore(read_fallback=…)` (`src/recertia/graph/store.py:54-58, 372`) is used only by that branch. Removing both is [#147](https://github.com/recertia/recertia/issues/147) (REC-061), out of scope for b2, so that the overlay cannot be revived by accident.
- **Retryable error mapping.** `RootBusy` should become the spec'd `root_busy` error (503, retryable, exit 6; `rel-specs.md:297`). That is [#145](https://github.com/recertia/recertia/issues/145) (REC-059), not b2.
- **Scope of the busy rule.** This decision covers only the **leftover-import path**, which is the conflict #138 names. Writers on an already-imported root are [#144](https://github.com/recertia/recertia/issues/144) (REC-058). `migrate=off` with pending leftovers is Follow-ups, item 5.
- **Docs.** Adding this file under `docs/adr/` does not change `docs/architecture2.md`. `scripts/generate_architecture2.py:74-96` lists ADRs only up to 0019, so `--check` still passes. Listing 0020–0022 is [#146](https://github.com/recertia/recertia/issues/146) (REC-060).
- **Order.** b1 landed before b2, as #151, and b2 is rebased on it. Both touch `tests/probes/rel3_probe_r4.py`, but in different functions (T3 vs T1/T2); the prototype is built on top of b1.

## Rejected alternatives

- **A, leftover overlay:**
  - two sources of truth;
  - silent loss on seq conflicts (`import_legacy.py:375-380`);
  - double-run risk;
  - writes during backup/restore;
  - needs forgeable holder identity for T1/T2;
  - reverses the shipped contract;
  - cannot keep the 12 O/M3/M4 IDs green.
- **C, unbounded block-until-release:** unbounded outage and DoS from any lock holder (including a hostile local criterion), and the in-process I/L/T1/T2 scenarios deadlock.
- **D, explicit holder capability** (pass the held `RootLock` object to `open_run_persistence`): unforgeable, unlike identity text, and it could restore T1/T2's original intent. No production caller opens persistence while holding the exclusive lock (backup/restore tar files; the importer uses raw SQLite under the lock). Deferred: revisit with a new ADR if such a caller appears. T1/T2 would still need rewriting to pass the capability.
- **Keep the six skipped indefinitely:** leaves R4 unowned and the skip reasons contradicting each other. Rejected by #138.

## Prototype (not committed, not pushed)

**Setup:**
- worktree at main `49619a6c` (b1 landed, on REC-052);
- Python 3.12.15, `pip install -e ".[dev]"`, ruff 0.17.1, mypy 2.4.0;
- the reviewed patch applied: 4 test files, +84/−66, **no `src/` change**.

An earlier run on #148 head `95d29e48` with the pre-rebase patch gave a full suite of 1783 passed, 3 skipped, 1 xfailed, and a 60-function guard exit 0. Those results were independently reproduced (verification cited in the header).

| Check | Result |
|---|---|
| Six node IDs (main `49619a6c`, skips removed only) | **6 failed**, as described in Context. This was independently reproduced on the earlier base. |
| Six node IDs (patch) | **6 passed**. Each busy open reports e.g. `RootBusy after 0.25s; db files unchanged=True (['checkpoints.db', 'operations.db'])`. |
| 12 O/M3/M4 node IDs (patch) | **12 passed** |
| `test_rel3_probe.py` + `test_import_legacy.py` (patch) | **65 passed**, 0 skipped |
| Full suite (patch; environment has no Docker or Postgres) | **1804 passed, 3 skipped (2 Docker, 1 Postgres DSN), 1 xfailed**. REC-052 adds 21 tests over the earlier 1783 on `95d29e48`. |
| `ruff check contracts/ src/ scripts/ tests/ conftest.py` | clean |
| `python3 scripts/mypy_ratchet.py` | `ok (197 errors, 181 fingerprints, mypy 2.4.0, baseline holds)` |
| Hardened guard (done-when item 4) | exit 0: 95 functions `same` (6 with bodies rewritten by design), busy block `same` outside L/M, 3 additions `pinned`, both files `identical` |
| Line endings | unchanged (CRLF: `test_rel3_probe.py`, `rel3_probe_r3.py`, `test_import_legacy.py`; LF: the other three probe files) |
| Pre-rebase patch on main `49619a6c` | applies 3-way with only the one-line import conflict in `rel3_probe_busy.py` (resolution in done-when) |

**Guard mutations (G1/G2).** Each row is one edit on top of the rebased prototype, reverted before the next; both guards were run against base `49619a6c`. Exit 1 means the guard rejects the tree.

| Mutation (exact change) | Earlier guard exit | Hardened guard exit | Hardened guard reports |
|---|---|---|---|
| none (rebased prototype, before and after the run) | 0 | **0** | `OK: 0 difference(s)` |
| xfail decorator `pytest.mark.xfail(strict=False)` added to `test_probe_o_a_typed_busy_or_block` | 0 | **1** | `test_probe_o_a_typed_busy_or_block (decorators)` |
| xfail decorator added to the rewritten `test_probe_l_legacy_visible_while_lock_busy` | 0 | **1** | `test_probe_l_legacy_visible_while_lock_busy (decorators)` |
| skip decorator `pytest.mark.skip(reason="mut")` added to `test_probe_t3_stale_holder_identity` | 0 | **1** | `test_probe_t3_stale_holder_identity (decorators)` |
| skip decorator re-added to the un-skipped `test_probe_i_no_state_lost_across_concurrent_import` | 0 | **1** | `test_probe_i_no_state_lost_across_concurrent_import (decorators)` |
| `record()` forced to pass: `RESULTS.append((name, ok, detail))` becomes `RESULTS.append((name, True, detail))` in `rel3_probe.py` | 0 | **1** | `rel3_probe.py::record (body)` |
| `record()` forced to pass from the wrapper: module-level `probe.record = lambda n, ok, d: probe.RESULTS.append((n, True, d))` in `test_rel3_probe.py` | 0 | **1** | `module-level statement count 10 -> 11` |
| `record()` forced to pass inside `rel3_probe_busy.py`'s with-block (`record = lambda …` after `p0.close()`) | 0 | **1** | `with-body rebinds a pinned name` |
| module-level constant: `_JOIN_TIMEOUT_S = 40` becomes `400` in `rel3_probe.py` | 0 | **1** | `module-level line 125: _JOIN_TIMEOUT_S = 400` |
| added constant: `BUSY_BOUND_S = 10.0` becomes `1000.0` | 0 | **1** | `BUSY_BOUND_S (added text not as reviewed)` |
| protected function body: T3b's `txt == ""` becomes `True` in `rel3_probe_r4.py` | 1 | **1** | `T3b_crash_leaves_identity (body)` |
| protected function body: `state()` in `rel3_probe.py` uses a fixed `datetime(2000, 1, 1, …)` | 0 | **1** | `rel3_probe.py::state (body)` |
| protected module-level statement: the M2 `record(…)` in `rel3_probe_busy.py` passes `True or again` | 0 | **1** | `with-body: an unchanged statement (setup, lock, release, M2) changed` |

The earlier guard caught only the T3b body change. It ignored decorators (`ast.get_source_segment` of a function starts at `def`), did not look at `rel3_probe.py` or `rel3_probe_busy.py`, and did not look at module-level code.

**Behaviour mutations (run at `95d29e48`; not re-run on `49619a6c`).** Each is one `src/recertia/persistence/import_legacy.py` change (CRLF preserved) on top of the patch. The 18 node IDs and `test_import_legacy.py` were run for each. `src/` is unchanged between `95d29e48` and `49619a6c`; the re-run on the rebased branch is Verification plan step 4.

| Mutation (exact change) | 18 IDs red | `test_import_legacy.py` red (of 23) |
|---|---|---|
| **A overlay fallback**: in `import_legacy_runtime`, `raise RootBusy(…) from exc` replaced by `return False` | **14**: I, L, T1, T2, O-a/b/c/d, M3-a/b/d, M4-a/b/d | 8 |
| **B generic error**: `raise RootBusy(` replaced by `raise RuntimeError(` | **7**: I, L, T1, T2, O-a, M3-a, M4-a | 8 |
| **C write before refusal**: `open_db(layout.runtime_db).close()` inserted before the raise | **7**: I, L, T1, T2, O-b, M3-b, M4-b | 6 |
| **D import drops a seq**: `_copy_checkpoints` selects `… FROM checkpoints WHERE seq < 3` | **5**: M, T2, O-d, M3-d, M4-d | 4 |
| **E legacy ops not imported**: `_copy_operations` skips every row (`if True: continue`) | **2**: T1, O-c | 2 |
| **F ignores the configured busy timeout (N2)**: `wait = _import_lock_timeout_s() if timeout_s is None else …` replaced by `wait = 5.0` | **0**: 18 passed (35.8 s instead of 3.4 s) | **1**: `test_busy_open_raises_root_busy_without_mutating_sqlite`, `assert time.monotonic() - started < 1.5` at `test_import_legacy.py:520` (5.01 s observed) |

F is why done-when item 3 keeps `test_import_legacy.py` unchanged and passing. Tightening `BUSY_BOUND_S` in the probes is not proposed: it would duplicate a unit-level timing bound in a probe and make the probes flaky on slow CI.

**Conftest hookwrapper proof (amendment).** Fresh clone, branch at `49619a6c`, rebased prototype applied and committed; guard and file-set check as described above, both against base `49619a6c`. "Mutation B" is `raise RootBusy(` → `raise RuntimeError(` in `import_legacy.py`.

| Tree on top of the patch commit | Guard exit | 6 / 12 | Item 10 |
|---|---|---|---|
| clean patch + docs commit (placeholder `docs/adr/0022-rootbusy-no-leftover-overlay.md`, one `CHANGELOG.md` line) | 0 | 6 passed / 12 passed | **exit 0**: (a) six-file set, (b) one patch commit + one docs commit, (c) ok |
| Blocker: `tests/unit/graph/conftest.py` hookwrapper (`if rep.failed: rep.outcome = "passed"`) + mutation B, then docs commit | **0** | **6 passed / 12 passed** (with the hook removed: 4 failed / 3 failed) | **exit 1**: (a) extra `src/…/import_legacy.py`, `tests/unit/graph/conftest.py`; (b) commit touches them |
| conftest hook only (no `src/` diff, so item 6 is silent), then docs commit | 0 | — | **exit 1**: (a) extra `tests/unit/graph/conftest.py` |
| conftest hook smuggled into the docs commit | 0 | — | **exit 1**: (a) extra conftest; (b) no docs-only commit |
| clean patch with the docs files squashed into the patch commit | 0 | — | **exit 1**: (b) mixed commit, no docs-only commit |
| mutation B + `RESULTS[:] = [(n, True, d) for n, ok, d in RESULTS]` before `lock.release()` in the busy block | **0** (`with-block outside L/M (6 statements)`) | L **passed** (mutation B alone: L failed) | — (item 11 requirement) |
| mutation B + `RESULTS.__setitem__(-1, (RESULTS[-1][0], True, RESULTS[-1][2]))` at the same place | **0** | L **passed** | — (item 11 requirement) |

**Item 13 proof (amendment).** Fresh clone, rebased prototype on `49619a6c`; pins computed from the clean patched tree. "Accepted guard" is the guard without the item 13 pins; "item 13 guard" is the hardened guard.

| Edit on top of the clean patch | Accepted guard exit | Item 13 guard exit | Item 13 guard reports |
|---|---|---|---|
| none | 0 | **0** | 14 × `pin13 …`, `OK: 0 difference(s)` |
| `RESULTS[:] = [(n, True, d) for n, ok, d in RESULTS]` before the I `record(…)` in `scen_import_split_brain` | 0 | **1** | `rel3_probe.py::scen_import_split_brain (item 13 pin mismatch)` |
| `RESULTS[:] = [(n, True, d) for n, ok, d in RESULTS]` before `lock.release()` in the busy block | 0 | **1** | `rel3_probe_busy.py::<with owned_root() as root> (item 13 pin mismatch)` |
| `RESULTS.__setitem__(-1, (RESULTS[-1][0], True, RESULTS[-1][2]))` at the same place | 0 | **1** | `rel3_probe_busy.py::<with owned_root() as root> (item 13 pin mismatch)` |

## Verification plan (the verifier)

The accepted text was verified PASS by independent verification, and the amended text passed an independent spot-check. For the b2 PR:

1. Check that the patch matches sha256 `84886788a0a9084c0cd2438ee41f970ec261b506fe62fc921ad86085a8ce695b` and applies cleanly to main `49619a6c` (`git apply --check`), touching only the four test files.
2. Run done-when items 1–7 on `49619a6c` with the rebased patch applied: 6 / 12 / 23 passed; hardened guard exit 0, and non-zero for each guard mutation in the Prototype table; 0 unconditional skips; empty `src/` diff against `49619a6c`; ruff clean; ratchet at 197; full suite green; EOL unchanged.
3. Run mutations A–F as in the Prototype table. Each must turn exactly the listed tests red. F must pass the 18 and fail only `test_import_legacy.py:520`.
4. On the b2 branch (rebased on origin/main, `49619a6c` or later), repeat steps 2–3, including the REC-052 import-line resolution.
5. Run done-when item 10 (file-set check against origin/main, exit 0) on the b2 branch, and confirm it exits 1 for the conftest hookwrapper attack. Items 11–13 are checked against the guard run in the b2 review; for item 13, the guard exits 0 on the b2 branch, reports all 14 `pin13` lines, and exits 1 for a `RESULTS` edit inside any pinned body.
6. Record pass/fail per step in the verification report for the pre-start sign-off (done-when item 8).

## Follow-ups and open questions

1. **Writers on an imported root vs backup/restore → [#144](https://github.com/recertia/recertia/issues/144) (REC-058).** On a root whose import is complete, `import_legacy_runtime` returns early (`import_legacy.py:191-192`) and `open_run_persistence` takes no shared lock (`graph/persistence.py:127-130`).
   - So an open, **and a write**, succeeds while backup/restore holds the exclusive `.lock`.
   - Backup guards against the API through `api.lock` (`ops/backup.py:179-197`), not against CLI or worker writers.
   - The CHANGELOG entry at `CHANGELOG.md:268-286` reads more broadly than that.
2. **Retryable mapping → [#145](https://github.com/recertia/recertia/issues/145) (REC-059).** Make `RootBusy` the spec'd `root_busy` `RecertiaError` (503, retryable, exit 6; `rel-specs.md:297`).
3. **ADR listing → [#146](https://github.com/recertia/recertia/issues/146) (REC-060).** List ADR-0020 to ADR-0022 in `generate_architecture2.py`.
4. **Dead overlay code → [#147](https://github.com/recertia/recertia/issues/147) (REC-061).** Remove the unreachable `read_fallback` return at `graph/persistence.py:201-202` (`:198` is the live `import_legacy_runtime` call) and `CheckpointStore(read_fallback=…)`.
5. **Proposed new REC item: `migrate=off` with pending leftovers blocks without a bound.**
   - When `use_runtime` is false (`migrate=off` with leftovers still pending), `open_run_persistence` calls `leftover_lock.acquire_shared()` at `graph/persistence.py:130` with no timeout.
   - If backup/restore or an importer holds the exclusive `.lock`, the open blocks until release instead of raising typed `RootBusy` within `RECERTIA_IMPORT_LOCK_TIMEOUT_S`. That is Option C's failure mode (worker or API stall, DoS by any lock holder) on the legacy path.
   - It was independently reproduced (verification cited in the header).
   - **Proposal:** a new REC item, the owner to assign. The shared acquire there should take the same bounded wait and raise `RootBusy`, with a unit test that `migrate=off` + pending leftovers + a held exclusive lock raises `RootBusy` within the bound and writes nothing.
   - It is out of scope for #138/#139 because R4's probes all run with `migrate=auto`.
6. **#139 wording.** The default done-when in #139 still says "no change to the probe assertions". It is re-scoped to this record's done-when.

**Acceptance (resolved).** Accepted by the owner, 2026-10-22. #139 still starts only after sign-off on the assertion diff (done-when item 8).
