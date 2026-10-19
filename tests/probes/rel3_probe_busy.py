"""#90 lock-busy path: leftover import while exclusive .lock is held."""
import tempfile
from pathlib import Path
from tests.probes.rel3_probe import (
    RESULTS, busy_open, leftover_digest, leftover_latest, holder_write, record, state, subprocess_exclusive,
)
from recertia.graph.persistence import open_run_persistence

root = Path(tempfile.mkdtemp())
p0 = open_run_persistence(root, migrate="off")
for s in (1, 2, 3): p0.checkpoints.save("legacy-run", s, f"n{s}", None, state("legacy-run"))
p0.close()
lock = subprocess_exclusive(root / ".lock")          # importer/backup busy
# MAIN: open_run_persistence while exclusive is held -> typed RootBusy, or block until release (5s)
seen = leftover_latest(root, "legacy-run")
before = leftover_digest(root)
ok_open, open_detail, released = busy_open(root, lock, timeout_s=5.0)
during = leftover_digest(root)
record("L open under exclusive RootLock: typed busy error or block-until-release", ok_open, open_detail)
# SECONDARY: leftover seqs stay on disk (read-only SELECT); busy open does not invent n1-new
record("L legacy state visible while root lock busy", seen == 3 and before == during,
       f"latest_seq('legacy-run') on busy-path process: {seen}; leftover digest before={before} during={during}")
# that process must not write somewhere else while exclusive is held (n1-new stays dropped)
wrote_holder = False
if not released:
    holder_write(root, "legacy-run", 4, "holder4")
    wrote_holder = True
    lock.release()
expected = [(1, "n1"), (2, "n2"), (3, "n3")]
if wrote_holder:
    expected.append((4, "holder4"))
# M: a later normal open must keep ALL state (pre-existing + holder's writes; nothing from the busy attempt)
try:
    p3 = open_run_persistence(root)
    hist = [(int(s_), n) for s_, n, *_ in p3.checkpoints.history("legacy-run")]
    p3.close()
    files = sorted(x.name for x in root.iterdir() if "db" in x.name)
    record("M later import after busy-path write keeps all state", hist == expected,
           f"history={hist}; expected={expected}; files={files}")
except Exception as exc:
    record("M later import after busy-path write keeps all state", False, f"open_run_persistence raises {type(exc).__name__}: {exc}")
# M2: recorded independently of M -- the root must open again on a second, fresh attempt
try:
    p4 = open_run_persistence(root); again = p4.checkpoints.latest_seq("legacy-run"); p4.close()
    record("M2 root still openable after conflict", True, f"opens; latest_seq={again}")
except Exception as exc2:
    record("M2 root still openable after conflict", False, f"still raises {type(exc2).__name__}: {exc2}")
print(f"SUMMARY: {sum(r[1] for r in RESULTS)} pass, {sum(not r[1] for r in RESULTS)} fail")
