"""R4 probes for 9b33b36's same-thread exclusive-holder exception. Each scenario uses a fresh root."""
import sys, os, tempfile, sqlite3, threading, asyncio, traceback, subprocess
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS, subprocess_exclusive
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

def T1_same_thread_ops_double_run():
    root = Path(tempfile.mkdtemp()); eff = legacy(root, op=True)
    holder = subprocess_exclusive(root / ".lock")          # importer/backup in a separate process
    busy = tw(lambda: open_run_persistence(root))
    holder.release()
    p = open_run_persistence(root)
    p.operations.run_once("legacy-run", 1, "solve", 0, lambda: eff.append("same-thread-rerun") or {"v": 2}); p.close()
    record("T1 same-thread holder: op done in legacy not re-executed", eff == ["legacy"], f"side effects={eff}; busy_open={busy}")
    root2 = Path(tempfile.mkdtemp()); legacy(root2, op=True)
    lk = RootLock(root2 / ".lock"); lk.try_exclusive()
    r = tw(lambda: open_run_persistence(root2)); lk.release()
    record("T1 same-thread open while holding exclusive is RootBusy", "Busy" in str(r), f"open={r}")

def T2_same_thread_ghost_write():
    root = Path(tempfile.mkdtemp()); legacy(root)
    holder = subprocess_exclusive(root / ".lock")
    busy = tw(lambda: open_run_persistence(root))
    holder.release()
    p = open_run_persistence(root)
    r = tw(lambda: p.checkpoints.save("legacy-run", 1, "GHOST", None, state("legacy-run"))); p.close()
    h = hist(root)
    record("T2 same-thread holder: write at a seq that exists in leftover is rejected, nothing lost",
           r != "ACCEPTED" and h == [(1, "n1"), (2, "n2"), (3, "n3")], f"save={r}; history after import={h}; busy_open={busy}")
    root2 = Path(tempfile.mkdtemp()); legacy(root2)
    lk = RootLock(root2 / ".lock"); lk.try_exclusive()
    r2 = tw(lambda: open_run_persistence(root2)); lk.release()
    record("T2 same-thread open while holding exclusive is RootBusy", "Busy" in str(r2), f"open={r2}")

def T3_stale_holder_identity():
    """Holder identity must not live in lock-file text. The helper is gone; leftover pid/tid
    without the flock never grants holder status."""
    try:
        from recertia.persistence.root_lock import exclusive_held_by_this_thread
        record("T3 exclusive_held_by_this_thread is absent from root_lock", False, "symbol still importable")
    except (ImportError, AttributeError) as e:
        record("T3 exclusive_held_by_this_thread is absent from root_lock", True, f"{type(e).__name__}: {e}")
    root = Path(tempfile.mkdtemp()); legacy(root)
    (root / ".lock").write_text(f"{os.getpid()} {threading.get_ident()}\n")   # leftover text, no flock held
    granted = False
    try:
        lk = RootLock(root / ".lock"); lk.try_exclusive(); lk.release()
    except Exception:
        granted = True
    p = open_run_persistence(root); imported = not (root / "checkpoints.db").is_file() or _complete(root)
    p.checkpoints.save("legacy-run", 4, "n4", None, state("legacy-run")); p.close()
    record("T3 identity text without the flock is not treated as holder", not granted and imported,
           f"leftover-text-grants-holder={granted}; import ran={imported}")

def _complete(root):
    from recertia.persistence.import_legacy import legacy_import_complete
    return legacy_import_complete(root / "runtime.db")

def T3b_crash_leaves_identity():
    root = Path(tempfile.mkdtemp())
    code = f"import os,sys;sys.path.insert(0,{repr(str(Path.cwd()/'src'))});from recertia.persistence.root_lock import RootLock;l=RootLock(__import__('pathlib').Path({repr(str(root/'.lock'))}));l.try_exclusive();os.kill(os.getpid(),9)"
    subprocess.run([sys.executable, "-c", code])
    txt = (root / ".lock").read_text().strip()
    record("T3b SIGKILLed holder leaves no identity in .lock", txt == "", f".lock text after SIGKILL={txt!r}")

def T4_asyncio_same_thread():
    """Backup/importer coroutine holds exclusive; another request coroutine on the SAME event-loop thread opens."""
    root = Path(tempfile.mkdtemp()); legacy(root)
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

def T5_threadpool_reuse():
    """Worker thread in a pool holds exclusive for task A; task B scheduled on the same worker opens."""
    from concurrent.futures import ThreadPoolExecutor
    root = Path(tempfile.mkdtemp()); legacy(root, op=True); eff = []
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
