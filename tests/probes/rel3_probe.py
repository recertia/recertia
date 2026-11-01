"""Adversarial REL-3 probe. Drives real RunLease / OperationLedger / CheckpointStore /
open_run_persistence / import_legacy code. Each scenario prints PASS or FAIL(BUG)."""
from __future__ import annotations
import multiprocessing as mp, os, sqlite3, sys, tempfile, threading, time, traceback
import contextlib, faulthandler, functools, signal
from datetime import datetime, timezone
from pathlib import Path

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph import lease as lease_mod
from recertia.graph.lease import RunLease, RunBusy
from recertia.graph.ops import OperationLedger
from recertia.graph.persistence import open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.persistence.root_lock import RootLock
try:
    from recertia.runtime.errors import LeaseLost
except ImportError:  # pre-#89 heads
    class LeaseLost(Exception): pass

RESULTS: list[tuple[str, bool, str]] = []
def record(name, ok, detail):
    RESULTS.append((name, ok, detail)); print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

def fresh_root(fn):
    """Run ``fn(root, *args)`` in a new temp dir that is removed when ``fn`` returns or raises.

    Cleanup runs on any ``BaseException``, not just ``Exception`` (#112): KeyboardInterrupt
    and pytest-timeout's ``signal``-method failure, which can land mid-``join()``, are
    cleaned up too. Before the dir is removed, every child the scenario started that is
    still alive (multiprocessing children and ``subprocess_exclusive`` holders) is killed
    and reaped, so none can keep writing into the dir while it is removed. If the removal
    still fails it is retried once; the scenario's own exception always propagates, with a
    removal failure added to it as a note, and a removal failure after a normal return is
    raised rather than hidden.

    Only children started while the scenario ran are reaped: children already alive when
    it was entered are left alone, as are other processes on the box.

    Not covered, by design:
    - SIGKILL, SIGTERM with no handler installed (``timeout(1)``, a CI cancel,
      pytest-timeout's ``thread`` method) and ``os._exit`` end the process before any
      cleanup can run, so they still leak the dir.
    - Grandchildren (children of a reaped child) are not reaped: there is no process-group
      kill, so nothing outside this process's own children is ever signalled. One that
      keeps writing makes the removal fail, which then takes the note path above.
    - A second interrupt (KeyboardInterrupt, pytest-timeout) that lands during the reap or
      the retry's sleep replaces the first: it propagates with the first as its
      ``__context__``, and that dir may be left behind.
    """
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with owned_root() as root:
            return fn(root, *args, **kwargs)
    return run

@contextlib.contextmanager
def owned_root():
    """``with owned_root() as root:`` gives the cleanup ``fresh_root`` does to a code block."""
    before = _live_children()
    tmp = tempfile.TemporaryDirectory()
    try:
        yield Path(tmp.name)
    except BaseException as exc:
        _release_root(tmp, before, exc)
        raise
    _release_root(tmp, before, None)

def _release_root(tmp, before, exc):
    """Reap the children started since ``before``, then remove ``tmp`` (one retry).

    With ``exc`` set (the scenario raised) nothing here may replace it: a failure is
    attached to ``exc`` as a note and the caller re-raises ``exc``.
    """
    try:
        _reap_children(before)
    except Exception as err:
        if exc is None:
            raise
        exc.add_note(f"fresh_root: reaping children failed: {err!r}")
    for attempt in (1, 2):
        try:
            tmp.cleanup()
            return
        except OSError as err:
            if attempt == 1:
                time.sleep(0.2)
                continue
            if exc is None:
                raise
            exc.add_note(f"fresh_root: could not remove {tmp.name}: {err!r}")

_HOLDERS: set = set()  # live subprocess_exclusive children, reaped by owned_root

def _live_children():
    return set(mp.active_children()) | set(_HOLDERS)

def _reap_children(before):
    new = [p for p in mp.active_children() if p not in before]
    _kill_children(new)
    for proc in [p for p in _HOLDERS if p not in before]:
        _stop_holder(proc)

def _stop_holder(proc):
    """Stop a ``subprocess_exclusive`` child and close its pipes."""
    try:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
        _HOLDERS.discard(proc)

# Both stay below the suite-wide pytest-timeout (60s, pyproject.toml) so the
# probe's own hang path (stack dumps, kill children) runs first, and children
# are daemonic so a pre-empted test cannot leave the session blocked at exit.
_BARRIER_TIMEOUT_S = 20
_JOIN_TIMEOUT_S = 40

def _spawn():
    return mp.get_context("spawn")

def _enable_fault_dumps():
    faulthandler.enable(all_threads=True)
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (AttributeError, RuntimeError, OSError, ValueError):
        pass

def _process_status(proc):
    return f"name={proc.name!r} pid={proc.pid} alive={proc.is_alive()} exitcode={proc.exitcode}"

def _dump_hung_children(procs):
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    for proc in procs:
        if proc.is_alive() and proc.pid:
            try:
                os.kill(proc.pid, signal.SIGUSR1)
            except OSError:
                pass
    time.sleep(0.2)

def _kill_children(procs):
    for proc in procs:
        if proc.is_alive():
            proc.terminate()
    for proc in procs:
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)

def _join_or_fail(label, procs, timeout=_JOIN_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    for proc in procs:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        proc.join(timeout=remaining)
    hung = [proc for proc in procs if proc.is_alive()]
    if not hung:
        return
    _dump_hung_children(procs)
    statuses = [_process_status(proc) for proc in procs]
    hung_names = [proc.name for proc in hung]
    _kill_children(procs)
    exits = [proc.exitcode for proc in procs]
    raise RuntimeError(
        f"{label} hung waiting for children; hung={hung_names}; "
        f"exitcodes={exits}; status={statuses}"
    )

def subprocess_exclusive(path):
    """Hold exclusive flock in a child process until ``.release()``.

    Used by probes I / L so the lock is a real other-process flock, not a
    same-thread identity bypass.
    """
    import subprocess
    path = Path(path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from recertia.persistence.root_lock import RootLock\n"
            "from pathlib import Path\n"
            "import sys\n"
            "l = RootLock(Path(sys.argv[1]))\n"
            "l.try_exclusive()\n"
            "sys.stdout.write('held\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.read()\n"
            "l.release()\n",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _HOLDERS.add(proc)
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if line.strip() != "held":
        err = proc.stderr.read() if proc.stderr is not None else ""
        _stop_holder(proc)
        raise RuntimeError(f"subprocess exclusive holder failed: {line!r} {err!r}")

    class _Holder:
        def release(self) -> None:
            _stop_holder(proc)

        def __del__(self) -> None:  # noqa: D105
            try:
                self.release()
            except Exception:
                pass

    return _Holder()


# ADR-0022 (REL-3 R4): an open that finds the root lock held is typed RootBusy, never an overlay.
BUSY_BOUND_S = 10.0   # RECERTIA_IMPORT_LOCK_TIMEOUT_S wait (tests/conftest.py: 0.25s; default 5s) + slack

def db_snapshot(root):
    """sha256 of every root-level file with "db" in its name (same notion as rel3_probe_r3)."""
    import hashlib
    return {x.name: hashlib.sha256(x.read_bytes()).hexdigest()[:16]
            for x in sorted(Path(root).iterdir()) if x.is_file() and "db" in x.name}

def busy_open(root):
    """Open ``root`` while another holder has its exclusive root lock (ADR-0022).

    Returns ``(ok, detail)``. ``ok`` is True only when the open raised the typed
    RootBusy within BUSY_BOUND_S and no db file changed or appeared. A generic
    error, or an open that succeeds, is a failure.
    """
    from recertia.persistence.root_lock import RootBusy
    before = db_snapshot(root)
    t0 = time.monotonic()
    try:
        p = open_run_persistence(root)
    except RootBusy as exc:
        dt = time.monotonic() - t0
        after = db_snapshot(root)
        ok = dt <= BUSY_BOUND_S and after == before
        return ok, f"RootBusy after {dt:.2f}s; db files unchanged={after == before} ({sorted(after)}); {exc}"
    except Exception as exc:
        return False, f"generic {type(exc).__name__} instead of RootBusy: {exc}"
    p.close()
    return False, "open SUCCEEDED while the root lock was held (busy window not honoured)"

def state(run_id):
    now = datetime.now(timezone.utc)
    return RunState(run_id=run_id, task=Task(task_id=run_id, request="x", task_class="repo-chore", submitted_at=now),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[TaskCriterion(id="c", kind="command", run="true", source="caller")], criteria_locked_at=now)

def effects(path):  # side-effect journal (O_APPEND lines)
    return path.read_text().splitlines() if path.exists() else []

def mk_lease(root, run="r1"):
    return RunLease(root / "locks" / f"{run}.lock", db_path=root / "runtime.db", tenant_id="local", run_id=run)

def holder_then_crash(root, journal, run):
    """Child: acquire lease like the engine, perform op once (effect lands), then crash (no release)."""
    _enable_fault_dumps()
    l = mk_lease(Path(root), run); tok = l.acquire()
    led = OperationLedger(Path(root) / "runtime.db")
    def eff():
        with open(journal, "a") as f: f.write(f"pid{os.getpid()} tok{tok} inc{l.incarnation}\n")
        return {"ok": True}
    led.run_once(run, 1, "solve", 0, eff, fencing_token=tok, incarnation=l.incarnation)
    os._exit(9)  # crash: flock freed by kernel, run_leases row left behind

# --- A: crash then resume must not re-run an applied op --------------------------------
@fresh_root
def scen_crash_resume(root):
    j = root / "journal"
    ctx = _spawn(); p = ctx.Process(target=holder_then_crash, args=(str(root), str(j), "r1"), name="rel3-holder-r1", daemon=True); p.start(); _join_or_fail("scen_crash_resume", [p])
    l = mk_lease(root); tok = l.acquire()
    led = OperationLedger(root / "runtime.db")
    def eff():
        with open(j, "a") as f: f.write(f"resume tok{tok} inc{l.incarnation}\n")
        return {"ok": True}
    led.run_once("r1", 1, "solve", 0, eff, fencing_token=tok, incarnation=l.incarnation)
    l.release()
    e = effects(j)
    record("A crash->resume op exactly-once", len(e) == 1, f"child exit={p.exitcode}; side effects={e}")

# --- A2: steal(inc2) does work, releases cleanly, then resume gets inc1 again -------------
@fresh_root
def scen_incarnation_reset(root):
    j = root / "journal"
    ctx = _spawn(); p = ctx.Process(target=holder_then_crash, args=(str(root), str(j), "r2"), name="rel3-holder-r2", daemon=True); p.start(); _join_or_fail("scen_incarnation_reset", [p])
    l = mk_lease(root, "r2"); l.acquire(); inc_after_crash = l.incarnation; l.release()
    l3 = mk_lease(root, "r2"); l3.acquire(); inc_next = l3.incarnation; l3.release()
    record("A2 incarnation never goes backwards", inc_next >= inc_after_crash,
           f"after crash inc={inc_after_crash}, next clean acquire inc={inc_next}")

# --- B: N concurrent runners race on one run ---------------------------------------------
def racer(root, journal, run, barrier):
    _enable_fault_dumps()
    l = mk_lease(Path(root), run); barrier.wait(timeout=_BARRIER_TIMEOUT_S)
    try: tok = l.acquire()
    except RunBusy:
        with open(journal + ".busy", "a") as f: f.write("busy\n"); return
    with open(journal + ".hold", "a") as f: f.write(f"in {time.monotonic():.6f}\n")
    led = OperationLedger(Path(root) / "runtime.db")
    def eff():
        with open(journal, "a") as f: f.write(f"pid{os.getpid()} tok{tok}\n")
        return {"ok": True}
    led.run_once(run, 1, "solve", 0, eff, fencing_token=tok, incarnation=l.incarnation)
    time.sleep(0.05)
    with open(journal + ".hold", "a") as f: f.write(f"out {time.monotonic():.6f}\n")
    l.release()

@fresh_root
def scen_race(root):
    j = root / "journal"; n = 8
    ctx = _spawn(); b = ctx.Barrier(n); ps = [ctx.Process(target=racer, args=(str(root), str(j), "r3", b), name=f"rel3-racer-{i}", daemon=True) for i in range(n)]
    [p.start() for p in ps]; _join_or_fail("scen_race", ps)
    ev = sorted((float(t), k) for k, t in (l.split() for l in effects(Path(str(j) + ".hold"))))
    depth = mx = 0
    for _, k in ev: depth += 1 if k == "in" else -1; mx = max(mx, depth)
    e = effects(j)
    toks = [int(x.split("tok")[1]) for x in e]
    record("B 8 racers: one holder at a time, effect once", mx <= 1 and len(e) == 1,
           f"max concurrent holders={mx}, effects={len(e)}, busy={len(effects(Path(str(j)+'.busy')))}")

# --- C: lock file unlinked while held -> second process also acquires (split brain) ------
def b_holder(root, q, go):
    _enable_fault_dumps()
    b = mk_lease(Path(root), "r4")
    try: q.put(("acq", b.acquire()))
    except RunBusy: q.put(("busy", None)); return
    go.wait(10); b.release()

@fresh_root
def scen_unlinked_lock(root):
    a = mk_lease(root, "r4"); ta = a.acquire()
    os.unlink(root / "locks" / "r4.lock")          # e.g. locks/ cleanup while a run is live
    ctx = mp.get_context("spawn"); q = ctx.Queue(); go = ctx.Event(); p = ctx.Process(target=b_holder, args=(str(root), q, go), name="rel3-b_holder", daemon=True); p.start()
    kind, tb = q.get(timeout=10)
    record("C lock-file unlink must not allow a 2nd holder", kind == "busy", f"holder A tok={ta} (alive); other process: {kind} tok={tb}")
    if kind == "acq":
        store = CheckpointStore(root / "runtime.db", lease_db=root / "runtime.db", tenant_id="local")
        try: store.save("r4", 1, "n", None, state("r4"), fencing_token=ta); ck = "ACCEPTED"
        except LeaseLost: ck = "rejected"
        led = OperationLedger(root / "runtime.db")
        led.run_once("r4", 1, "solve", 7, lambda: {"stale": True}, fencing_token=ta, incarnation=1)
        ok_ops = led.get("r4", 1, "solve", 7, incarnation=1)[0]
        from recertia.ledger import HashChainLedger
        try:
            HashChainLedger(root / "ledger.jsonl").append(actor="stale", action=__import__("typing").get_args(__import__("contracts.ledger", fromlist=["LedgerAction"]).LedgerAction)[0], target="r4"); lg = "ACCEPTED"
        except Exception as exc: lg = f"rejected/err {type(exc).__name__}"
        record("D stale token: checkpoint write rejected", ck == "rejected", f"CheckpointStore.save stale tok {ta} (current {tb}): {ck}")
        record("D2 stale token: operations write rejected", not ok_ops, f"OperationLedger.run_once stale tok {ta}: {'ACCEPTED' if ok_ops else 'rejected'}")
        record("D3 stale token: hash-chain ledger append rejected", lg != "ACCEPTED", f"HashChainLedger.append by stale holder (no token parameter at all): {lg}")
    go.set(); _join_or_fail("scen_unlinked_lock", [p]); a.release()

# --- E: fencing TOCTOU (steal lands between check and write) ------------------------------
@fresh_root
def scen_toctou(root):
    a = mk_lease(root, "r5"); ta = a.acquire()
    store = CheckpointStore(root / "runtime.db", lease_db=root / "runtime.db", tenant_id="local")
    real = lease_mod.require_lease_token
    def check_then_steal(*args, **kw):
        real(*args, **kw)
        # steal happens right after the check (simulate holder crash + new runner): bump row
        c = sqlite3.connect(root / "runtime.db"); c.execute("UPDATE run_leases SET fencing_token=fencing_token+1, incarnation=incarnation+1"); c.commit(); c.close()
    lease_mod.require_lease_token = check_then_steal
    try:
        store.save("r5", 1, "n", None, state("r5"), fencing_token=ta); res = "ACCEPTED"
    except LeaseLost: res = "rejected"
    finally:
        lease_mod.require_lease_token = real
        store.close()
    with contextlib.closing(sqlite3.connect(root / "runtime.db")) as c:
        cur = c.execute("SELECT fencing_token FROM run_leases").fetchone()[0]
    record("E check-then-write is atomic", res == "rejected", f"write with tok {ta} after row moved to {cur}: {res}")
    a.release()

# --- F: token reuse after clean release + lock dir wiped ----------------------------------
@fresh_root
def scen_token_reuse(root):
    toks = []
    for _ in range(3):
        l = mk_lease(root, "r6"); toks.append(l.acquire()); l.release()
    import shutil; shutil.rmtree(root / "locks")
    l = mk_lease(root, "r6"); t = l.acquire(); l.release()
    record("F tokens strictly monotonic across restart", t > max(toks), f"tokens {toks} then after locks/ wipe: {t}")

# --- G: fail-open when runtime.db/run_leases absent -------------------------------------
@fresh_root
def scen_fail_open(root):
    store = CheckpointStore(root / "ck.db", lease_db=root / "missing-runtime.db", tenant_id="local")
    try: store.save("r7", 1, "n", None, state("r7"), fencing_token=999); res = "ACCEPTED"
    except LeaseLost: res = "rejected"
    finally: store.close()
    record("G no lease row/db => write rejected", res == "rejected", f"bogus token 999 with no runtime.db: {res}")

# --- H: operations table rebuild crash midway -------------------------------------------
@fresh_root
def scen_ops_rebuild_crash(root):
    db = root / "operations.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE operations (run_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, node TEXT NOT NULL, op_seq INTEGER NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY (run_id, attempt_no, node, op_seq))")
    c.execute("INSERT INTO operations VALUES ('old',1,'n',0,'{\"v\":1}')"); c.commit()
    # crash state after the rebuild's first (auto-committed) CREATE TABLE operations_rel3
    from recertia.graph import ops as ops_mod
    c.execute(ops_mod._OPERATIONS_DDL.replace("operations", "operations_rel3", 1)); c.commit(); c.close()
    try:
        with contextlib.closing(OperationLedger(db)) as led: got = led.get("old", 1, "n", 0)
        record("H ops rebuild resumable after crash", got[0], f"after restart: {got}")
    except Exception as exc:
        record("H ops rebuild resumable after crash", False, f"restart raises {type(exc).__name__}: {exc}")

# --- I: legacy import while another process still on legacy -> lost write ----------------
@fresh_root
def scen_import_split_brain(root):
    # old-format state
    p0 = open_run_persistence(root, migrate="off"); p0.checkpoints.save("old-run", 1, "n", None, state("old-run")); p0.close()
    lock = subprocess_exclusive(root / ".lock")          # e.g. backup/another importer holds it
    busy_ok, busy = busy_open(root)                      # ADR-0022: p2 is refused (typed RootBusy), writes nothing
    lock.release()
    p1 = open_run_persistence(root)                                 # imports + retires legacy files
    p2 = open_run_persistence(root)                                 # p2 retries after release, on runtime.db
    p2.checkpoints.save("p2-run", 1, "n", None, state("p2-run"))
    p2.close(); p1.close()
    p3 = open_run_persistence(root)
    ids = sorted(p3.checkpoints.list_run_ids()) if hasattr(p3.checkpoints, "list_run_ids") else None
    p3.close()
    record("I no state lost across concurrent import", busy_ok and ids == ["old-run", "p2-run"],
           f"busy open: {busy}; runs visible after re-open: {ids}; files: {sorted(x.name for x in root.iterdir())}")

# --- J: import idempotent; crash after commit before retire; re-run ----------------------
@fresh_root
def scen_import_idempotent(root):
    p0 = open_run_persistence(root, migrate="off")
    for i in range(5): p0.checkpoints.save("legacy", i + 1, "n", None, state("legacy"))
    p0.operations.put("legacy", 1, "n", 0, {"v": 1}); p0.close()
    from recertia.persistence import import_legacy as il
    real = il._retire_legacy_files
    il._retire_legacy_files = lambda layout: (_ for _ in ()).throw(SystemExit("crash before retire"))
    try: open_run_persistence(root)
    except SystemExit: pass
    finally: il._retire_legacy_files = real
    for _ in range(2): open_run_persistence(root).close()
    c = sqlite3.connect(root / "runtime.db")
    n_ck = c.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id='legacy'").fetchone()[0]
    n_op = c.execute("SELECT COUNT(*) FROM operations WHERE run_id='legacy'").fetchone()[0]
    ver = [r[0] for r in c.execute("SELECT version FROM schema_migrations")]
    c.close()
    left = sorted(x.name for x in root.iterdir() if "db" in x.name)
    record("J import idempotent + resumable", n_ck == 5 and n_op == 1, f"checkpoints={n_ck} ops={n_op} schema_migrations={ver} files={left}")
    # rollback story: migrate=off after import
    p = open_run_persistence(root, migrate="off"); latest = p.checkpoints.latest_seq("legacy"); p.close()
    record("K migrate=off after import still sees state (rollback)", latest == 5, f"latest_seq('legacy') under migrate=off: {latest}")

if __name__ == "__main__":
    try:
        mp.set_start_method("spawn")
    except RuntimeError:
        pass
    for fn in [scen_crash_resume, scen_incarnation_reset, scen_race, scen_unlinked_lock, scen_toctou,
               scen_token_reuse, scen_fail_open, scen_ops_rebuild_crash, scen_import_split_brain, scen_import_idempotent]:
        try: fn()
        except Exception as exc: record(fn.__name__, False, f"probe error {type(exc).__name__}: {exc}"); traceback.print_exc()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\nSUMMARY: {len(RESULTS)-len(bad)} pass, {len(bad)} fail")
