"""#90 lock-busy path. ADR-0022 (REL-3 R4): a busy open is typed RootBusy and writes nothing;
after release the import keeps all legacy state exactly."""
import sys, tempfile, sqlite3
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS, owned_root, busy_open
from recertia.graph.persistence import open_run_persistence
from recertia.persistence.root_lock import RootLock

with owned_root() as root:  # reaps the lock holder before removing the dir
    p0 = open_run_persistence(root, migrate="off")
    for s in (1, 2, 3): p0.checkpoints.save("legacy-run", s, f"n{s}", None, state("legacy-run"))
    p0.close()
    EXPECTED = [(1, "n1"), (2, "n2"), (3, "n3")]
    lock = __import__("tests.probes.rel3_probe", fromlist=["subprocess_exclusive"]).subprocess_exclusive(root / ".lock")          # importer/backup busy
    busy_ok, busy = busy_open(root)                                 # ADR-0022: typed RootBusy, no write
    c = sqlite3.connect(f"file:{root / 'checkpoints.db'}?mode=ro", uri=True)
    try:
        legacy = [(int(s), n) for s, n in c.execute(
            "SELECT seq, node FROM checkpoints WHERE run_id='legacy-run' ORDER BY seq")]
    finally:
        c.close()
    record("L busy open is typed RootBusy, writes nothing; legacy state intact", busy_ok and legacy == EXPECTED,
           f"busy open: {busy}; leftover checkpoints.db rows={legacy}")
    lock.release()
    # M: the first normal open after release imports and must keep ALL legacy state exactly
    try:
        p3 = open_run_persistence(root)
        hist = [(int(s), n) for s, n, *_ in p3.checkpoints.history("legacy-run")]; p3.close()
        files = sorted(x.name for x in root.iterdir() if "db" in x.name)
        record("M import after the refused busy open keeps all legacy state exactly", hist == EXPECTED,
               f"history after import={hist}; files={files}")
    except Exception as exc:
        record("M import after the refused busy open keeps all legacy state exactly", False,
               f"open_run_persistence raises {type(exc).__name__}: {exc}")
    # M2: recorded independently of M -- the root must open again on a second, fresh attempt
    try:
        p4 = open_run_persistence(root); again = p4.checkpoints.latest_seq("legacy-run"); p4.close()
        record("M2 root still openable after conflict", True, f"opens; latest_seq={again}")
    except Exception as exc2:
        record("M2 root still openable after conflict", False, f"still raises {type(exc2).__name__}: {exc2}")
print(f"SUMMARY: {sum(r[1] for r in RESULTS)} pass, {sum(not r[1] for r in RESULTS)} fail")
