"""R15 and G17: the improvement plane does not import T3, the solver, or the stores."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "recertia" / "rsi"
_FORBIDDEN_ROOTS = (
    "recertia.solver",
    "recertia.ledger",
    "recertia.memory",
    "recertia.distill",
    "recertia.governance",
)


def _imports(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
    return found


def _forbidden(module: str) -> bool:
    for root in _FORBIDDEN_ROOTS:
        if module == root or module.startswith(root + "."):
            return True
    return "t3_config" in module or "torch" in module


class ImportBoundaryTest(unittest.TestCase):
    def test_improvement_plane_does_not_import_t3_or_production(self):
        for path in sorted(ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for module in _imports(tree):
                self.assertFalse(_forbidden(module), f"{path.name} imports {module}")

    def test_bridge_does_not_call_the_disabled_sandbox(self):
        bridge = ROOT.parent / "nodes" / "rsi_bridge.py"
        tree = ast.parse(bridge.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotEqual(node.id, "run_sandboxed")
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, "run_sandboxed")

    def test_t3_writer_is_refused(self):
        from recertia.rsi.t3_config import set_ablation_rate

        with self.assertRaises(PermissionError):
            set_ablation_rate(0.0)


if __name__ == "__main__":
    unittest.main()
