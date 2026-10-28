"""R3 adversarial probes for #89 (fail-closed fencing) and #90 (busy overlay, api.lock, copy rollback).
Every scenario runs in its own fresh root and records independently."""
import sys, sqlite3, traceback, typing
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS, fresh_root
from recertia.graph.persistence import open_run_persistence
from recertia.graph.lease import RunLease
try:
    from recertia.runtime.errors import LeaseLost
except ImportError:
    class LeaseLost(Exception): pass
ACT = typing.get_args(__import__("contracts.ledger", fromlist=["LedgerAction"]).LedgerAction)[0]
HAS_IMPORT = "migrate" in open_run_persistence.__code__.co_varnames

def lease(root, run, ttl=30):
    return RunLease(root / "locks" / f"{run}.lock", db_path=root / "runtime.db", tenant_id="local", run_id=run, ttl_s=ttl)

def tw(fn):
    try: fn(); return "ACCEPTED"
    except Exception as e: return f"rejected({type(e).__name__})"

def scen(fn):
    try: fn()
    except Exception as e:
        traceback.print_exc(); record(fn.__name__, False, f"probe error {type(e).__name__}: {e}")

# ---------- #89: fail-closed must not wedge legitimate runs ----------
@fresh_root
def P_fresh_run_no_lease(root):
    p = open_run_persistence(root); run = "fresh"
    r = [tw(lambda: p.checkpoints.save(run, 1, "n", None, state(run))),
         tw(lambda: p.operations.run_once(run, 1, "solve", 0, lambda: 1)),
         tw(lambda: p.ledger.append(actor="x", action=ACT, target=run))]
    record("P fresh never-leased run, token-less writes allowed", r == ["ACCEPTED"]*3, f"ck/op/lg={r}")

@fresh_root
def P3_after_clean_release(root):
    p = open_run_persistence(root); run = "done"
    a = lease(root, run); t = a.acquire(); p.checkpoints.save(run, 1, "n", None, state(run), fencing_token=t); a.release()
    lg0 = tw(lambda: p.ledger.append(actor="reviewer", action=ACT, target=run))
    record("P3 token-less ledger append on a RELEASED run is rejected", lg0 != "ACCEPTED", f"token-less ledger={lg0}")
    # positive control: same append on a run that was never leased stays allowed
    ctl = tw(lambda: p.ledger.append(actor="reviewer", action=ACT, target="never-leased-run"))
    record("P3c control: token-less ledger append on a never-leased run allowed", ctl == "ACCEPTED", f"token-less ledger={ctl}")

@fresh_root
def P4_reacquire_after_release(root):
    p = open_run_persistence(root); run = "again"
    a = lease(root, run); t = a.acquire(); a.release()
    b = lease(root, run); t2 = b.acquire()
    r = tw(lambda: p.checkpoints.save(run, 1, "n", None, state(run), fencing_token=t2)); b.release()
    record("P4 re-acquire after release writes with new token", r == "ACCEPTED" and t2 > t, f"t={t} t2={t2} write={r}")

# ---------- #90 ----------
def _legacy(root, seqs=(1, 2, 3), run="legacy-run", op=False):
    p0 = open_run_persistence(root, migrate="off")
    for s in seqs: p0.checkpoints.save(run, s, f"n{s}", None, state(run))
    eff = []
    if op: p0.operations.run_once(run, 1, "solve", 0, lambda: eff.append("legacy") or {"v": 1})
    p0.close(); return eff

# ---- busy-window behavior (fallback deleted): open under exclusive RootLock must either raise a
# typed *Busy* error within BUSY_T, or block and succeed only after release. Nothing may be written
# during the busy window, no side effect may run, and after release a fresh open sees exactly the
# pre-existing state plus the holder's writes (no loss, no double-run).
import hashlib, json, multiprocessing as mp, time
BUSY_T = 5.0      # bounded wait for the typed busy error / max time the child may block while held
AFTER_T = 20.0    # max time to finish after the holder releases

def _documented_busy_types():
    names = set()
    for mod in ("recertia.runtime.errors", "recertia.persistence.root_lock", "recertia.persistence.import_legacy"):
        try:
            m = __import__(mod, fromlist=["x"])
        except Exception:
            continue
        for n in dir(m):
            obj = getattr(m, n)
            if isinstance(obj, type) and issubclass(obj, BaseException) and "busy" in n.lower():
                names.add(n)
    return names

def _is_busy(exc):
    return "Busy" in type(exc).__name__ or type(exc).__name__ in _documented_busy_types()

def _snapshot(root):
    snap = {}
    for x in sorted(root.iterdir()):
        if x.is_file() and ("db" in x.name):
            snap[x.name] = hashlib.sha256(x.read_bytes()).hexdigest()[:16]
    return snap

def _holder_write(root, run, seq, node):
    """The RootLock holder (e.g. importer/backup) writes directly to legacy checkpoints.db."""
    c = sqlite3.connect(root / "checkpoints.db")
    cols = [r[1] for r in c.execute("PRAGMA table_info(checkpoints)")]
    row = dict(zip(cols, c.execute("SELECT * FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run,)).fetchone()))
    row.update(seq=seq, node=node)
    c.execute(f"INSERT INTO checkpoints ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", [row[k] for k in cols])
    c.commit(); c.close()

def _busy_child(root, kind, effects_path, q):
    root = Path(root); t0 = time.monotonic()
    def eff(tag):
        with open(effects_path, "a") as f: f.write(tag + "\n")
    try:
        p = open_run_persistence(root)
    except Exception as e:
        q.put(("open_raised", type(e).__name__, str(e)[:200], time.monotonic() - t0, _is_busy(e))); return
    q.put(("opened", None, None, time.monotonic() - t0, None))
    res = []
    try:
        if kind == "O":
            res.append(tw(lambda: p.operations.run_once("legacy-run", 1, "solve", 0, lambda: eff("busy-rerun") or {"v": 2})))
        elif kind == "M3":
            for s_ in (1, 2, 3): res.append(tw(lambda: p.checkpoints.save("legacy-run", s_, f"new{s_}", None, state("legacy-run"))))
        elif kind == "M4":
            res.append(tw(lambda: p.checkpoints.save("legacy-run", 1, "BUSY", None, state("legacy-run"))))
        p.close()
    except Exception as e:
        res.append(f"error {type(e).__name__}: {e}")
    q.put(("actions", res, None, time.monotonic() - t0, None))

def _append(path, text):
    with open(path, "a") as f: f.write(text)

@fresh_root
def _busy_scenario(tmp, kind):
    from recertia.persistence.root_lock import RootLock
    root = tmp / "root"; root.mkdir(); effects = tmp / "root.effects"
    leg_eff = _legacy(root, op=(kind == "O"))
    with open(effects, "w") as f:
        for e in leg_eff: f.write(e + "\n")
    lock = RootLock(root / ".lock"); lock.try_exclusive()
    _holder_write(root, "legacy-run", 4, "holder4")                      # holder's own write
    expected = [(1, "n1"), (2, "n2"), (3, "n3"), (4, "holder4")]
    before = _snapshot(root)
    ctx = mp.get_context("spawn"); q = ctx.Queue()
    pr = ctx.Process(target=_busy_child, args=(str(root), kind, str(effects), q)); pr.start()
    try:
        first = q.get(timeout=BUSY_T)
    except Exception:
        first = None                                                     # still blocked -> mode (b) candidate
    during = _snapshot(root)
    fx_during = effects.read_text().split()
    outcome, ok_mode = "", False
    if first is None:
        lock.release(); released = True
        try:
            first = q.get(timeout=AFTER_T)
        except Exception:
            first = None
        if first is None:
            outcome = f"HANG: no result {BUSY_T}s held + {AFTER_T}s after release"
        elif first[0] == "opened":
            ok_mode, outcome = True, f"(b) blocked while held, opened {first[3]:.2f}s after start, after release"
        else:
            outcome = f"raised {first[1]} only after release ({first[2]})"
    else:
        released = False
        if first[0] == "open_raised" and first[4]:
            ok_mode, outcome = True, f"(a) typed busy error {first[1]} after {first[3]:.2f}s"
        elif first[0] == "open_raised":
            outcome = f"generic error {first[1]}: {first[2]}"
        else:
            outcome = f"open SUCCEEDED while lock held ({first[3]:.2f}s) -- busy window not honoured"
    if not released:
        lock.release()
    acts = None
    if first is not None and first[0] == "opened":
        try: acts = q.get(timeout=AFTER_T)[1]
        except Exception: acts = "HANG in actions"
    pr.join(timeout=AFTER_T)
    if pr.is_alive(): pr.kill(); outcome += "; child killed (hang)"
    record(f"{kind}-a open under exclusive RootLock: typed busy error or block-until-release", ok_mode, outcome + (f"; child actions={acts}" if acts else ""))
    record(f"{kind}-b nothing written during busy window", before == during, f"before={before} during={during}")
    try:
        f = open_run_persistence(root)
        hist = [(int(s_), n) for s_, n, *_ in f.checkpoints.history("legacy-run")]
        if kind == "O":
            f.operations.run_once("legacy-run", 1, "solve", 0, lambda: _append(effects, "fresh-rerun\n") or {"v": 3})
        f.close(); fresh = "opens"
    except Exception as e:
        hist, fresh = None, f"{type(e).__name__}: {e}"
    fx = effects.read_text().split()
    exp_fx = ["legacy"] if kind == "O" else []
    record(f"{kind}-c no side effect during busy window, none double-run after", fx_during == exp_fx and fx == exp_fx, f"during={fx_during} final={fx} expected={exp_fx}")
    record(f"{kind}-d after release fresh open = pre-existing + holder writes exactly", hist == expected, f"fresh open {fresh}; history={hist}; expected={expected}")

def O_busy_ops_exactly_once():
    _busy_scenario("O")

def M3_full_restart_conflict():
    _busy_scenario("M3")

def M4_busy_write_overwritten():
    _busy_scenario("M4")

@fresh_root
def Q_off_after_import_sees_new_writes(root):
    _legacy(root)
    p = open_run_persistence(root); p.checkpoints.save("legacy-run", 4, "post-import", None, state("legacy-run")); p.close()
    o = open_run_persistence(root, migrate="off"); s = o.checkpoints.latest_seq("legacy-run")
    o.checkpoints.save("legacy-run", 4, "off-write", None, state("legacy-run")) if s == 3 else None; o.close()
    record("Q migrate=off (rollback) sees writes made after import", s == 4, f"off latest_seq={s} (runtime has 4)")
    r = []
    for i in range(2):
        try: a = open_run_persistence(root); r.append(f"opens seq={a.checkpoints.latest_seq('legacy-run')}"); a.close()
        except Exception as e: r.append(f"{type(e).__name__}: {e}")
    record("Q2 roll forward again (auto) after off-mode writes", all(x.startswith("opens") for x in r), "; ".join(r))

@fresh_root
def R_reimport_every_open(root):
    _legacy(root)
    open_run_persistence(root).close()
    from recertia.persistence.import_legacy import legacy_sqlite_pending
    from recertia.persistence.layout import RuntimeLayout
    lay = RuntimeLayout(root).local_runs() if hasattr(RuntimeLayout(root), "local_runs") else None
    pend = legacy_sqlite_pending(lay) if lay else "n/a"
    record("R legacy not 'pending' after a completed import", pend is False, f"legacy_sqlite_pending after import={pend}; files={sorted(x.name for x in root.iterdir() if 'db' in x.name)}")

@fresh_root
def S_backup_refused_while_api_up(tmp):
    from recertia.api import create_app
    from recertia.ops import backup
    root = tmp / "root"; app = create_app(root=root)
    fn = getattr(backup, "backup_tree", None) or getattr(backup, "create_backup", None)
    out = tmp / "b.tgz"
    try: fn(root, out); r = "backup ALLOWED while API up"
    except Exception as e: r = f"refused: {e}"
    try: backup.restore_tree(out, root, overwrite=True); r2 = "restore ALLOWED over live API root"
    except Exception as e: r2 = f"refused: {type(e).__name__}: {e}"
    record("S backup/restore still refused while API process is up", r.startswith("refused") and r2.startswith("refused"), f"{r}; {r2}")

if __name__ == "__main__":
    for f in (P_fresh_run_no_lease, P3_after_clean_release, P4_reacquire_after_release):
        scen(f)
    if HAS_IMPORT:
        for f in (O_busy_ops_exactly_once, M3_full_restart_conflict, M4_busy_write_overwritten, Q_off_after_import_sees_new_writes, R_reimport_every_open, S_backup_refused_while_api_up):
            scen(f)
    bad = [r for r in RESULTS if not r[1]]
    print(f"\nSUMMARY: {len(RESULTS)-len(bad)} pass, {len(bad)} fail")
