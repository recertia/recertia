"""R4 probes for 9b33b36's same-thread exclusive-holder exception. Each scenario uses a fresh root."""
import sys, os, sqlite3, threading, asyncio, traceback, subprocess
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS, fresh_root
from recertia.graph.persistence import open_run_persistence
from recertia.persistence.root_lock import RootLock

def legacy(root, op=False):
    p = open_run_persistence(root, migrate="off")
    for s in (1, 2, 3): p.checkpoints.save("legacy-run", s, f"n{s}", None, state("legacy-run"))
    eff = []
    if op: p.operations.run_once("legacy-run", 1, "solve", 0, lambda: eff.append("legacy") or {"v": 1})
    p.close(); return eff

def hist(root):
    p = open_run_persistence(root); h = [(int(s), n) for s, n, *_ in p.checkpoints.history("legacy-run")]; p.close(); return h

def tw(fn):
    try: fn(); return "ACCEPTED"
    except Exception as e: return f"rejected({type(e).__name__})"

def scen(f):
    try: f()
    except Exception as e: traceback.print_exc(); record(f.__name__, False, f"probe error {type(e).__name__}: {e}")

@fresh_root
def T1_same_thread_ops_double_run(root):
    eff = legacy(root, op=True)
    lk = RootLock(root / ".lock"); lk.try_exclusive()          # e.g. importer/backup in this thread
    p = open_run_persistence(root)
    p.operations.run_once("legacy-run", 1, "solve", 0, lambda: eff.append("same-thread-rerun") or {"v": 2}); p.close(); lk.release()
    record("T1 same-thread holder: op done in legacy not re-executed", eff == ["legacy"], f"side effects={eff}")

@fresh_root
def T2_same_thread_ghost_write(root):
    legacy(root)
    lk = RootLock(root / ".lock"); lk.try_exclusive()
    p = open_run_persistence(root)
    r = tw(lambda: p.checkpoints.save("legacy-run", 1, "GHOST", None, state("legacy-run"))); p.close(); lk.release()
    h = hist(root)
    record("T2 same-thread holder: write at a seq that exists in leftover is rejected, nothing lost",
           r != "ACCEPTED" and h == [(1, "n1"), (2, "n2"), (3, "n3")], f"save={r}; history after import={h}")

@fresh_root
def T3_stale_holder_identity(root):
    """Holder identity lives in the lock file text. A crashed holder (SIGKILL) leaves it behind;
    a later process with the same pid/thread-ident (PID 1 in a container, pthread ident reuse) is
    treated as the holder WITHOUT holding the flock."""
    legacy(root)
    (root / ".lock").write_text(f"{os.getpid()} {threading.get_ident()}\n")   # leftover text, no flock held
    from recertia.persistence.root_lock import exclusive_held_by_this_thread
    spoof = exclusive_held_by_this_thread(root / ".lock")
    p = open_run_persistence(root); imported = not (root / "checkpoints.db").is_file() or _complete(root)
    p.checkpoints.save("legacy-run", 4, "n4", None, state("legacy-run")); p.close()
    record("T3 identity text without the flock is not treated as holder", not spoof and imported,
           f"exclusive_held_by_this_thread={spoof}; import ran={imported}")

def _complete(root):
    from recertia.persistence.import_legacy import legacy_import_complete
    return legacy_import_complete(root / "runtime.db")

@fresh_root
def T3b_crash_leaves_identity(root):
    code = f"import os,sys;sys.path.insert(0,{repr(str(Path.cwd()/'src'))});from recertia.persistence.root_lock import RootLock;l=RootLock(__import__('pathlib').Path({repr(str(root/'.lock'))}));l.try_exclusive();os.kill(os.getpid(),9)"
    subprocess.run([sys.executable, "-c", code])
    txt = (root / ".lock").read_text().strip()
    record("T3b SIGKILLed holder leaves no identity in .lock", txt == "", f".lock text after SIGKILL={txt!r}")

@fresh_root
def T4_asyncio_same_thread(root):
    """Backup/importer coroutine holds exclusive; another request coroutine on the SAME event-loop thread opens."""
    legacy(root)
    out = {}
    async def holder(ev, done):
        lk = RootLock(root / ".lock"); lk.try_exclusive(); ev.set(); await done.wait(); lk.release()
    async def request(ev, done):
        await ev.wait()
        try:
            p = open_run_persistence(root); out["open"] = "opened (no RootBusy)"
            out["w"] = tw(lambda: p.checkpoints.save("legacy-run", 1, "REQ", None, state("legacy-run"))); p.close()
        except Exception as e: out["open"] = type(e).__name__
        done.set()
    async def main():
        ev, done = asyncio.Event(), asyncio.Event(); await asyncio.gather(holder(ev, done), request(ev, done))
    asyncio.run(main())
    record("T4 asyncio: unrelated coroutine on the holder's thread gets RootBusy, not the holder exception",
           out.get("open", "").endswith("Busy"), f"{out}; history after={hist(root)}")

@fresh_root
def T5_threadpool_reuse(root):
    """Worker thread in a pool holds exclusive for task A; task B scheduled on the same worker opens."""
    from concurrent.futures import ThreadPoolExecutor
    legacy(root, op=True); eff = []
    with ThreadPoolExecutor(max_workers=1) as ex:
        lk = ex.submit(lambda: (lambda l: (l.try_exclusive(), l)[1])(RootLock(root / ".lock"))).result()   # task A takes lock, returns w/o release
        def task_b():
            try:
                p = open_run_persistence(root); p.operations.run_once("legacy-run", 1, "solve", 0, lambda: eff.append("taskB") or 1); p.close(); return "opened"
            except Exception as e: return type(e).__name__
        r = ex.submit(task_b).result()
        ex.submit(lk.release).result()
    record("T5 thread-pool: a different task on the holder's worker thread is not the holder", r.endswith("Busy") and not eff, f"task B={r}; side effects={eff}")

if __name__ == "__main__":
    for f in (T1_same_thread_ops_double_run, T2_same_thread_ghost_write, T3_stale_holder_identity, T3b_crash_leaves_identity, T4_asyncio_same_thread, T5_threadpool_reuse):
        scen(f)
    bad = [r for r in RESULTS if not r[1]]; print(f"\nSUMMARY: {len(RESULTS)-len(bad)} pass, {len(bad)} fail")
