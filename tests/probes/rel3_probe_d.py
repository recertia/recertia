"""REL-3 probe D (standalone): force a LEGITIMATE steal via lease expiry, then the stale
holder attempts writes on all three fenced stores. Drives real RunLease / open_run_persistence
(CheckpointStore, OperationLedger, HashChainLedger). Every stale write must be rejected.

Steal recipe (the only way a *live* holder can lose a flock lease): holder A acquires with a
short TTL and stops heartbeating (hung); its lock file is replaced (e.g. locks/ cleanup or a
second host); after the TTL a different process B acquires -> run_leases says steal
(incarnation+1, token+1). A is still alive and tries to write with its old token.
"""
from __future__ import annotations
import multiprocessing as mp, os, sys, tempfile, time, typing
from pathlib import Path

from recertia.graph.lease import RunLease, RunBusy
from recertia.graph.persistence import open_run_persistence
try:
    from recertia.runtime.errors import LeaseLost
except ImportError:
    class LeaseLost(Exception): pass

from datetime import datetime, timezone
from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task

def state(run_id):  # same RunState builder as rel3_probe.py (inlined so this file stands alone)
    now = datetime.now(timezone.utc)
    return RunState(run_id=run_id, task=Task(task_id=run_id, request="x", task_class="repo-chore", submitted_at=now),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[TaskCriterion(id="c", kind="command", run="true", source="caller")], criteria_locked_at=now)

TTL = 0.5
RESULTS: list[tuple[str, bool, str]] = []
def record(name, ok, detail):
    RESULTS.append((name, ok, detail)); print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

def lease(root, run):
    return RunLease(root / "locks" / f"{run}.lock", db_path=root / "runtime.db", tenant_id="local", run_id=run, ttl_s=TTL)

def thief(root, run, q, release_after):
    b = lease(Path(root), run)
    try: tok = b.acquire()
    except RunBusy as exc: q.put(("busy", None, None)); return
    q.put(("acq", tok, b.incarnation))
    if release_after: b.release()
    else: time.sleep(3); b.release()

def try_write(label, fn):
    try: fn(); return "ACCEPTED"
    except LeaseLost: return "rejected(LeaseLost)"
    except Exception as exc: return f"rejected({type(exc).__name__})"

def scenario(mode):
    with tempfile.TemporaryDirectory() as tmp:
        _scenario(Path(tmp), mode)

def _scenario(root, mode):
    run = f"d-{mode}"
    persist = open_run_persistence(root)
    a = lease(root, run); ta = a.acquire(); inc_a = a.incarnation
    persist.checkpoints.save(run, 1, "n", None, state(run), fencing_token=ta)   # legit write
    os.unlink(root / "locks" / f"{run}.lock"); time.sleep(TTL + 0.3)             # A hung, lock replaced, TTL passes
    ctx = mp.get_context("spawn"); q = ctx.Queue()
    p = ctx.Process(target=thief, args=(str(root), run, q, mode == "after-release")); p.start()
    kind, tb, inc_b = q.get(timeout=20)
    if mode == "after-release": p.join()
    stole = kind == "acq" and tb is not None and tb > ta and inc_b > inc_a
    record(f"D0[{mode}] legitimate steal happened", stole, f"A tok={ta} inc={inc_a}; B {kind} tok={tb} inc={inc_b}")
    if not stole:
        p.join(); return
    a.heartbeat()  # stale holder must not be able to refresh the row
    effect = []
    act = typing.get_args(__import__("contracts.ledger", fromlist=["LedgerAction"]).LedgerAction)[0]
    r1 = try_write("ck", lambda: persist.checkpoints.save(run, 2, "n", None, state(run), fencing_token=ta))
    r2 = try_write("op", lambda: persist.operations.run_once(run, 1, "solve", 0, lambda: effect.append(1) or {"stale": True}, fencing_token=ta, incarnation=inc_a))
    r3 = try_write("lg", lambda: persist.ledger.append(actor="stale", action=act, target=run, fencing_token=ta))
    record(f"D[{mode}] stale checkpoint write rejected", r1 != "ACCEPTED", r1)
    record(f"D2[{mode}] stale operation-ledger write rejected (no side effect)", r2 != "ACCEPTED" and not effect, f"{r2}; side effects run={len(effect)}")
    record(f"D3[{mode}] stale hash-chain append rejected", r3 != "ACCEPTED", r3)
    # same three with no token at all (a stale caller that drops its token)
    n1 = try_write("ck0", lambda: persist.checkpoints.save(run, 3, "n", None, state(run)))
    n2 = try_write("op0", lambda: persist.operations.run_once(run, 1, "solve", 1, lambda: effect.append(2) or {"stale": True}))
    n3 = try_write("lg0", lambda: persist.ledger.append(actor="stale", action=act, target=run))
    record(f"D4[{mode}] token-less writes for a stolen run rejected", "ACCEPTED" not in (n1, n2, n3), f"checkpoint={n1}, operation={n2}, ledger={n3}")
    import sqlite3
    c = sqlite3.connect(root / "runtime.db"); row = c.execute("SELECT fencing_token, incarnation, status FROM run_leases WHERE run_id=?", (run,)).fetchone(); c.close()
    record(f"D5[{mode}] lease row still owned by B", row is not None and row[0] == tb, f"run_leases row={row}")
    p.join(); persist.close()

if __name__ == "__main__":
    for mode in ("while-held", "after-release"):
        try: scenario(mode)
        except Exception as exc:
            import traceback; traceback.print_exc(); record(f"scenario[{mode}]", False, f"probe error {type(exc).__name__}: {exc}")
    bad = [r for r in RESULTS if not r[1]]
    print(f"\nSUMMARY: {len(RESULTS)-len(bad)} pass, {len(bad)} fail")
