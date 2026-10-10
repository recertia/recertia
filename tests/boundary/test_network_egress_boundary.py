"""Only allowlisted modules may open network connections or spawn shells.

Agent-system analog of a "no live order / wallet client" guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NET_ROOTS = {"httpx", "requests", "socket", "aiohttp", "websockets", "web3", "eth_account"}
NET_MODULES = {"urllib.request", "http.client"}
ALLOWED = {
    "src/recertia/solver/providers.py",
    "src/recertia/solver/registry.py",
    "src/recertia/api/console_auth.py",
    "src/recertia/jobs/arxiv.py",
    "src/recertia/jobs/arxiv_pdf.py",
}


def _files() -> list[Path]:
    return sorted(p for d in ("src", "contracts") for p in (REPO / d).rglob("*.py"))


def _imported(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    return []


def test_network_imports_are_allowlisted() -> None:
    bad = []
    for p in _files():
        rel = p.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names = _imported(node)
            if rel not in ALLOWED and any(
                n in NET_MODULES or n.split(".")[0] in NET_ROOTS for n in names
            ):
                bad.append(f"{rel}:{node.lineno}")  # type: ignore[attr-defined]
    assert not bad, f"unallowlisted network egress: {bad}"


def test_allowlist_has_no_stale_entries() -> None:
    for rel in ALLOWED:
        assert (REPO / rel).exists(), f"stale allowlist entry: {rel}"


# shell=True is allowed only at reviewed sites. Keyed by file; the count pins how many.
SHELL_TRUE_ALLOWED = {
    # local executor, gated by LocalExecutionCapability (marked recertia-security-ok)
    "src/recertia/solver/container.py": 1,
}


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _shell_true_sites(tree: ast.Module) -> list[int]:
    """shell=True as a call kwarg, a {"shell": True} dict literal, or dict(shell=True)."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            lines += [node.lineno for kw in node.keywords if kw.arg == "shell" and _is_true(kw.value)]
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "shell" and _is_true(v):
                    lines.append(k.lineno)
    return lines


def test_shell_true_only_at_reviewed_sites() -> None:
    found: dict[str, int] = {}
    for p in _files():
        rel = p.relative_to(REPO).as_posix()
        n = len(_shell_true_sites(ast.parse(p.read_text(encoding="utf-8"))))
        if n:
            found[rel] = n
    assert found == SHELL_TRUE_ALLOWED, f"shell=True sites changed: {found}"


def test_detector_sees_dict_and_kwarg_forms() -> None:
    src = 'subprocess.run(c, shell=True)\nkw = {"shell": True}\nkw2 = dict(shell=True)\n'
    assert len(_shell_true_sites(ast.parse(src))) == 3


def test_no_os_system_or_popen() -> None:
    bad = []
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            f = getattr(node, "func", None)
            if (
                isinstance(f, ast.Attribute)
                and f.attr in {"system", "popen"}
                and isinstance(f.value, ast.Name)
                and f.value.id == "os"
            ):
                bad.append(f"{p}:{node.lineno}")  # type: ignore[attr-defined]
    assert not bad, bad
