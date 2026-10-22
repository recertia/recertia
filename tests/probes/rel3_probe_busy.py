"""#90 lock-busy path: legacy state not yet imported while a writer is on runtime.db."""
import sys, tempfile, sqlite3
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS
from recertia.graph.persistence import open_run_persistence
from recertia.persistence.root_lock import RootLock

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    p0 = open_run_persistence(root, migrate="off")
    for s in (1, 2, 3): p0.checkpoints.save("legacy-run", s, f"n{s}", None, state("legacy-run"))
    p0.close()
    lock = __import__("tests.probes.rel3_probe", fromlist=["subprocess_exclusive"]).subprocess_exclusive(root / ".lock")          # importer/backup busy
    p2 = open_run_persistence(root)                                 # busy -> runtime.db, no import
    seen = p2.checkpoints.latest_seq("legacy-run")
    record("L legacy state visible while root lock busy", seen == 3, f"latest_seq('legacy-run') on busy-path process: {seen}")
    # that process "resumes"/restarts the run from scratch and writes seq 1 (different content)
    p2.checkpoints.save("legacy-run", 1, "n1-new", None, state("legacy-run")); p2.close()
    lock.release()
    # M: a later normal open must keep ALL state (legacy seq 1-3 plus the busy-path write)
    try:
        p3 = open_run_persistence(root); latest = p3.checkpoints.latest_seq("legacy-run"); p3.close()
        files = sorted(x.name for x in root.iterdir() if "db" in x.name)
        record("M later import after busy-path write keeps all state", latest == 3, f"latest_seq after import={latest}; files={files}")
    except Exception as exc:
        record("M later import after busy-path write keeps all state", False, f"open_run_persistence raises {type(exc).__name__}: {exc}")
    # M2: recorded independently of M -- the root must open again on a second, fresh attempt
    try:
        p4 = open_run_persistence(root); again = p4.checkpoints.latest_seq("legacy-run"); p4.close()
        record("M2 root still openable after conflict", True, f"opens; latest_seq={again}")
    except Exception as exc2:
        record("M2 root still openable after conflict", False, f"still raises {type(exc2).__name__}: {exc2}")
print(f"SUMMARY: {sum(r[1] for r in RESULTS)} pass, {sum(not r[1] for r in RESULTS)} fail")
