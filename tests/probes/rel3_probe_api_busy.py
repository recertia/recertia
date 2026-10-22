import sys, tempfile
from pathlib import Path
from tests.probes.rel3_probe import state, record, RESULTS
from recertia.graph.persistence import open_run_persistence
from recertia.persistence.layout import RuntimeLayout
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp); lay = RuntimeLayout(root).tenant("t1")
    p = open_run_persistence(lay, migrate="off"); p.checkpoints.save("legacy-t1", 1, "n", None, state("legacy-t1")); p.close()
    from recertia.api import create_app
    app = create_app(root=root)          # takes shared root/.lock for the process lifetime
    p = open_run_persistence(lay); seen = p.checkpoints.latest_seq("legacy-t1"); p.close()
    files = sorted(x.name for x in (root / "runs" / "t1").iterdir() if "db" in x.name)
    record("N API process sees legacy tenant run after upgrade", seen == 1, f"latest_seq={seen}; tenant files={files}")
