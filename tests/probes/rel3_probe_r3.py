"""R3 adversarial probes for #89 (fail-closed fencing) and #90 (busy overlay, api.lock, copy rollback).
Every scenario runs in its own fresh root and records independently."""
import sys, tempfile, sqlite3, traceback, typing
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS
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
def P_fresh_run_no_lease():
    root = Path(tempfile.mkdtemp()); p = open_run_persistence(root); run = "fresh"
    r = [tw(lambda: p.checkpoints.save(run, 1, "n", None, state(run))),
         tw(lambda: p.operations.run_once(run, 1, "solve", 0, lambda: 1)),
         tw(lambda: p.ledger.append(actor="x", action=ACT, target=run))]
    record("P fresh never-leased run, token-less writes allowed", r == ["ACCEPTED"]*3, f"ck/op/lg={r}")

def P3_after_clean_release():
    root = Path(tempfile.mkdtemp()); p = open_run_persistence(root); run = "done"
    a = lease(root, run); t = a.acquire(); p.checkpoints.save(run, 1, "n", None, state(run), fencing_token=t); a.release()
    lg0 = tw(lambda: p.ledger.append(actor="reviewer", action=ACT, target=run))
    ck0 = tw(lambda: p.checkpoints.save(run, 2, "n", None, state(run)))
    lgt = tw(lambda: p.ledger.append(actor="reviewer", action=ACT, target=run, fencing_token=t))
    record("P3 post-completion token-less ledger append on a released run", lg0 == "ACCEPTED", f"token-less ledger={lg0}, token-less checkpoint={ck0}, released holder's own token ledger={lgt}")

def P4_reacquire_after_release():
    root = Path(tempfile.mkdtemp()); p = open_run_persistence(root); run = "again"
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

def O_busy_ops_exactly_once():
    from recertia.persistence.root_lock import RootLock
    root = Path(tempfile.mkdtemp()); eff = _legacy(root, op=True)
    lock = RootLock(root / ".lock"); lock.try_exclusive()
    p = open_run_persistence(root)
    p.operations.run_once("legacy-run", 1, "solve", 0, lambda: eff.append("busy-rerun") or {"v": 2}); p.close(); lock.release()
    record("O busy path: op already done in legacy is not re-executed", eff == ["legacy"], f"side effects={eff}")

def M3_full_restart_conflict():
    from recertia.persistence.root_lock import RootLock
    root = Path(tempfile.mkdtemp()); _legacy(root)
    lock = RootLock(root / ".lock"); lock.try_exclusive(); p = open_run_persistence(root)
    for s in (1, 2, 3): p.checkpoints.save("legacy-run", s, f"new{s}", None, state("legacy-run"))
    p.close(); lock.release()
    r = []
    for i in range(2):
        try: q = open_run_persistence(root); r.append(f"opens seq={q.checkpoints.latest_seq('legacy-run')}"); q.close()
        except Exception as e: r.append(f"{type(e).__name__}: {e}")
    record("M3 busy-path rewrite of same seqs does not wedge root", all(x.startswith("opens") for x in r), "; ".join(r))

def M4_busy_write_overwritten():
    from recertia.persistence.root_lock import RootLock
    root = Path(tempfile.mkdtemp()); _legacy(root)
    lock = RootLock(root / ".lock"); lock.try_exclusive(); p = open_run_persistence(root)
    p.checkpoints.save("legacy-run", 1, "BUSY", None, state("legacy-run")); p.close(); lock.release()
    q = open_run_persistence(root); h = [(s, n) for s, n, *_ in q.checkpoints.history("legacy-run")]; q.close()
    record("M4 busy-path write is not silently overwritten by import", ("1", "BUSY") in [(str(s), n) for s, n in h] or len(h) > 3, f"history={h}")

def Q_off_after_import_sees_new_writes():
    root = Path(tempfile.mkdtemp()); _legacy(root)
    p = open_run_persistence(root); p.checkpoints.save("legacy-run", 4, "post-import", None, state("legacy-run")); p.close()
    o = open_run_persistence(root, migrate="off"); s = o.checkpoints.latest_seq("legacy-run")
    o.checkpoints.save("legacy-run", 4, "off-write", None, state("legacy-run")) if s == 3 else None; o.close()
    record("Q migrate=off (rollback) sees writes made after import", s == 4, f"off latest_seq={s} (runtime has 4)")
    r = []
    for i in range(2):
        try: a = open_run_persistence(root); r.append(f"opens seq={a.checkpoints.latest_seq('legacy-run')}"); a.close()
        except Exception as e: r.append(f"{type(e).__name__}: {e}")
    record("Q2 roll forward again (auto) after off-mode writes", all(x.startswith("opens") for x in r), "; ".join(r))

def R_reimport_every_open():
    root = Path(tempfile.mkdtemp()); _legacy(root)
    open_run_persistence(root).close()
    from recertia.persistence.import_legacy import legacy_sqlite_pending
    from recertia.persistence.layout import RuntimeLayout
    lay = RuntimeLayout(root).local_runs() if hasattr(RuntimeLayout(root), "local_runs") else None
    pend = legacy_sqlite_pending(lay) if lay else "n/a"
    record("R legacy not 'pending' after a completed import", pend is False, f"legacy_sqlite_pending after import={pend}; files={sorted(x.name for x in root.iterdir() if 'db' in x.name)}")

def S_backup_refused_while_api_up():
    from recertia.api import create_app
    from recertia.ops import backup
    root = Path(tempfile.mkdtemp()) / "root"; app = create_app(root=root)
    fn = getattr(backup, "backup_tree", None) or getattr(backup, "create_backup", None)
    out = Path(tempfile.mkdtemp()) / "b.tgz"
    try: fn(root, out); r = "backup ALLOWED while API up"
    except Exception as e: r = f"refused: {e}"
    rs = Path(tempfile.mkdtemp())
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
