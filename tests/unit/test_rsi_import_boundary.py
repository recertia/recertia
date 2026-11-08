"""R15: improvement-plane modules must not import T3 config."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "recertia" / "rsi"
SCAN = [
    "hygiene.py",
    "leakage.py",
    "scoring.py",
    "attribution.py",
    "pipeline.py",
    "world.py",
    "models.py",
    "ledger.py",
]


class ImportBoundaryTest(unittest.TestCase):
    def test_improvement_plane_does_not_import_t3(self):
        for name in SCAN:
            tree = ast.parse((ROOT / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    self.assertNotIn("t3_config", module, name)
                    self.assertNotIn("torch", module, name)

    def test_t3_writer_is_refused(self):
        from recertia.rsi.t3_config import set_ablation_rate

        with self.assertRaises(PermissionError):
            set_ablation_rate(0.0)


if __name__ == "__main__":
    unittest.main()
