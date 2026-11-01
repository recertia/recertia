"""Every ``open_db(`` and ``sqlite3.connect(`` call in ``src/recertia`` closes what it opens.

A call passes when it has one of two shapes:

(i) it is the argument of ``closing(...)`` in a ``with`` item;
(ii) it is the whole value of ``x = <open>`` or ``x: T = <open>``, the statement right after
     that assignment is a ``try:``, and that ``try``'s ``finally:`` has ``x.close()`` as one of
     its own statements (not inside an ``if`` or any other block).

Anything else needs an entry in ``ALLOWLIST``, keyed on the file and the enclosing function,
with a reason and the tests that cover it as pytest node ids. The guard checks that each named
test file and test function exists and that every entry still matches a call.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "recertia"

OPEN_DB = "open_db"
CONNECT = "sqlite3.connect"

_PROBE = (
    "ro probe: connect alone in try/except Error -> return False; the next statement is try/finally close"
)
_PROBE_TESTS = "tests/unit/test_open_failure_paths_close.py"
_AUDIT_TESTS = "tests/unit/test_promotion_audit_close_pins.py"
_READER_TESTS = "tests/unit/test_open_failure_paths_close.py"


@dataclass(frozen=True)
class Entry:
    path: str  # relative to src/recertia
    function: str  # qualified name of the enclosing function
    call: str  # OPEN_DB or CONNECT
    reason: str
    tests: tuple[str, ...]  # pytest node ids, relative to the repository root


ALLOWLIST: tuple[Entry, ...] = (
    Entry(
        "persistence/db.py",
        "open_db",
        CONNECT,
        "returns the connection; its own try/except BaseException closes it when setup fails",
        (
            "tests/unit/persistence/test_open_db.py::test_open_db_base_exception_in_setup_closes_once_and_reraises",
            "tests/unit/persistence/test_open_db.py::test_open_db_setup_error_survives_a_failing_close",
        ),
    ),
    Entry(
        "persistence/db.py",
        "open_db_with_init",
        OPEN_DB,
        "returns the connection; its own try/except BaseException closes it when init raises",
        (
            "tests/unit/persistence/test_open_db_with_init.py::test_exception_in_init_closes_once_and_reraises_the_same_object",
            "tests/unit/persistence/test_open_db_with_init.py::test_base_exception_in_init_closes_once_and_reraises",
        ),
    ),
    Entry(
        "api/auth.py",
        "ApiKeyStore._connect",
        OPEN_DB,
        "factory: every caller closes the connection it returns",
        (
            "tests/unit/test_api_key_store_closes.py::test_every_api_key_store_operation_closes_its_connections",
            "tests/unit/test_api_key_store_closes.py::test_error_inside_the_block_still_closes_and_propagates",
        ),
    ),
    Entry(
        "review/promotion_audit.py",
        "OperatorAuditStore._connect",
        OPEN_DB,
        "factory: __init__, record() and read() close the connection in try/finally",
        (
            f"{_AUDIT_TESTS}::test_init_failure_after_open_closes_and_raises_sink_unavailable",
            f"{_AUDIT_TESTS}::test_record_failure_after_open_closes_and_raises_sink_unavailable",
            f"{_AUDIT_TESTS}::test_read_failure_after_open_closes_and_propagates",
        ),
    ),
    Entry(
        "memory/affordance/__init__.py",
        "AffordanceStore._write_conn",
        OPEN_DB,
        "first-use writer: kept as self._writer on success; except BaseException closes it when setup fails",
        (f"{_PROBE_TESTS}::test_affordance_writer_setup_failure_closes_and_propagates",),
    ),
    Entry(
        "persistence/idempotency.py",
        "IdempotencyStore._writer",
        OPEN_DB,
        "first-use writer: kept as self._conn on success; except BaseException closes it when setup fails",
        (f"{_PROBE_TESTS}::test_idempotency_writer_setup_failure_closes_and_propagates",),
    ),
    Entry(
        "persistence/import_legacy.py",
        "legacy_import_complete",
        CONNECT,
        _PROBE,
        (f"{_PROBE_TESTS}::test_legacy_import_complete_probe_closes_on_error",),
    ),
    Entry(
        "persistence/import_legacy.py",
        "sqlite_has_table",
        CONNECT,
        _PROBE,
        (f"{_PROBE_TESTS}::test_sqlite_has_table_probe_closes_on_error",),
    ),
    Entry(
        "persistence/import_legacy.py",
        "checkpoint_has_run",
        CONNECT,
        _PROBE,
        (f"{_PROBE_TESTS}::test_checkpoint_has_run_probe_closes_on_error",),
    ),
    Entry(
        "memory/affordance/__init__.py",
        "AffordanceStore._open_reader",
        CONNECT,
        "ownership transfers to caller on success (AffordanceStore._read closes it in try/finally)",
        (
            f"{_READER_TESTS}::test_affordance_reader_isolation_level_failure_closes",
            f"{_READER_TESTS}::test_affordance_reader_operational_error_closes_and_propagates",
            f"{_READER_TESTS}::test_affordance_reader_no_such_table_closes_and_returns_none",
            f"{_READER_TESTS}::test_affordance_reader_other_error_closes_and_propagates",
            f"{_READER_TESTS}::test_affordance_read_closes_the_reader_it_gets",
        ),
    ),
)


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    function: str
    call: str
    shape: str  # "closing", "try/finally", "allowlist" or "" when none applies
    why: str = ""

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.call}( in {self.function or '<module>'}: {self.why}"


def _open_kind(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open_db":
        return OPEN_DB
    if isinstance(func, ast.Attribute):
        if func.attr == "open_db":
            return OPEN_DB
        if func.attr == "connect" and isinstance(func.value, ast.Name) and func.value.id == "sqlite3":
            return CONNECT
    return None


def _is_close_of(stmt: ast.stmt, name: str) -> bool:
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    call = stmt.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "close"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == name
        and not call.args
        and not call.keywords
    )


def _is_closing(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "closing") or (
        isinstance(func, ast.Attribute) and func.attr == "closing"
    )


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.parents: dict[ast.AST, ast.AST] = {}
        self.sites: list[Site] = []

    def scan(self, tree: ast.AST) -> list[Site]:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.visit(tree)
        return self.sites

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        kind = _open_kind(node)
        if kind is not None:
            shape, why = self._shape(node)
            self.sites.append(Site(self.path, node.lineno, ".".join(self.scope), kind, shape, why))
        self.generic_visit(node)

    def _shape(self, node: ast.Call) -> tuple[str, str]:
        parent = self.parents.get(node)
        if parent is not None and _is_closing(parent) and isinstance(self.parents.get(parent), ast.withitem):
            return "closing", ""
        if isinstance(parent, ast.Assign) and parent.value is node:
            if len(parent.targets) != 1 or not isinstance(parent.targets[0], ast.Name):
                return "", "assigned to something other than one plain name"
            name = parent.targets[0].id
        elif isinstance(parent, ast.AnnAssign) and parent.value is node:
            if not isinstance(parent.target, ast.Name):
                return "", "assigned to something other than one plain name"
            name = parent.target.id
        else:
            return "", "not in `with closing(...)` and not assigned to a name"
        following = self._next_statement(parent)
        if not isinstance(following, ast.Try):
            return "", "the next statement after the open is not `try:`"
        if not any(_is_close_of(stmt, name) for stmt in following.finalbody):
            return "", f"the following `try:` has no `{name}.close()` directly in its `finally:`"
        return "try/finally", ""

    def _next_statement(self, stmt: ast.stmt) -> ast.stmt | None:
        holder = self.parents.get(stmt)
        for field in ("body", "orelse", "finalbody", "handlers"):
            block = getattr(holder, field, None)
            if isinstance(block, list) and stmt in block:
                index = block.index(stmt)
                return block[index + 1] if index + 1 < len(block) else None
        return None


def scan_source(source: str, path: str) -> list[Site]:
    return _Scanner(path).scan(ast.parse(source))


def scan_tree(root: Path) -> list[Site]:
    sites: list[Site] = []
    for file in sorted(root.rglob("*.py")):
        sites.extend(scan_source(file.read_text(encoding="utf-8"), file.relative_to(root).as_posix()))
    return sites


def _test_names(file: Path) -> set[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    names.add(f"{node.name}::{item.name}")
    return names


def check_allowlist(entries: Iterable[Entry], sites: Iterable[Site], repo: Path) -> list[str]:
    """Problems with the allowlist itself: no reason, no tests, a missing test, a stale entry."""

    problems: list[str] = []
    keys = {(s.path, s.function, s.call) for s in sites}
    for entry in entries:
        label = f"{entry.path} {entry.function} {entry.call}"
        if not entry.reason.strip():
            problems.append(f"{label}: no reason")
        if not entry.tests:
            problems.append(f"{label}: names no test")
        for node_id in entry.tests:
            file, sep, name = node_id.partition("::")
            test_file = repo / file
            if not sep or not name:
                problems.append(f"{label}: {node_id!r} is not a pytest node id")
            elif not test_file.is_file():
                problems.append(f"{label}: test file {file} does not exist")
            elif name.split("[", 1)[0] not in _test_names(test_file):
                problems.append(f"{label}: node id {node_id} does not exist")
        if (entry.path, entry.function, entry.call) not in keys:
            problems.append(f"{label}: stale, matches no call")
    return problems


def violations(sites: Iterable[Site], entries: Iterable[Entry]) -> list[Site]:
    allowed = {(e.path, e.function, e.call) for e in entries}
    return [s for s in sites if not s.shape and (s.path, s.function, s.call) not in allowed]


def guard(root: Path, repo: Path, entries: Iterable[Entry]) -> list[str]:
    entries = tuple(entries)
    sites = scan_tree(root)
    return [str(s) for s in violations(sites, entries)] + check_allowlist(entries, sites, repo)


def test_every_src_open_call_closes_what_it_opens() -> None:
    problems = guard(SRC, REPO, ALLOWLIST)
    assert not problems, "\n".join(problems)


def test_the_guard_sees_both_calls_in_src() -> None:
    calls = {s.call for s in scan_tree(SRC)}
    assert calls == {OPEN_DB, CONNECT}


# -- fixtures: the shapes ------------------------------------------------------------------


def _shapes(source: str) -> list[tuple[str, str]]:
    return [(s.call, s.shape) for s in scan_source(source, "m.py")]


def test_closing_in_a_with_item_passes() -> None:
    source = (
        "def f(p):\n"
        "    with closing(open_db(p)) as c:\n"
        "        c.execute('x')\n"
        "    with closing(sqlite3.connect(p)) as d:\n"
        "        d.execute('x')\n"
    )
    assert _shapes(source) == [(OPEN_DB, "closing"), (CONNECT, "closing")]


def test_open_then_try_finally_close_passes() -> None:
    source = (
        "def f(p):\n    c = open_db(p)\n    try:\n        c.execute('x')\n    finally:\n        c.close()\n"
    )
    assert _shapes(source) == [(OPEN_DB, "try/finally")]


def test_try_except_else_finally_close_passes() -> None:
    source = (
        "def f(p):\n"
        "    c = sqlite3.connect(p)\n"
        "    try:\n"
        "        c.execute('x')\n"
        "    except sqlite3.Error:\n"
        "        pass\n"
        "    else:\n"
        "        c.commit()\n"
        "    finally:\n"
        "        c.close()\n"
    )
    assert _shapes(source) == [(CONNECT, "try/finally")]


def test_annotated_assignment_passes() -> None:
    source = (
        "def f(p):\n"
        "    c: sqlite3.Connection = open_db(p)\n"
        "    try:\n"
        "        c.execute('x')\n"
        "    finally:\n"
        "        c.close()\n"
    )
    assert _shapes(source) == [(OPEN_DB, "try/finally")]


@pytest.mark.parametrize("condition", ["owned", "c is not None"])
def test_conditional_close_in_finally_fails(condition: str) -> None:
    source = (
        "def f(p, owned):\n"
        "    c = open_db(p)\n"
        "    try:\n"
        "        c.execute('x')\n"
        "    finally:\n"
        f"        if {condition}:\n"
        "            c.close()\n"
    )
    assert _shapes(source) == [(OPEN_DB, "")]


def test_statement_between_open_and_try_fails() -> None:
    source = (
        "def f(p):\n"
        "    c = open_db(p)\n"
        "    owned = True\n"
        "    try:\n"
        "        c.execute('x')\n"
        "    finally:\n"
        "        c.close()\n"
    )
    assert _shapes(source) == [(OPEN_DB, "")]


def test_finally_closing_a_different_name_fails() -> None:
    source = (
        "def f(p, other):\n"
        "    c = open_db(p)\n"
        "    try:\n"
        "        c.execute('x')\n"
        "    finally:\n"
        "        other.close()\n"
    )
    assert _shapes(source) == [(OPEN_DB, "")]


@pytest.mark.parametrize(
    ("line", "call"),
    [("return open_db(p)", OPEN_DB), ("return sqlite3.connect(p)", CONNECT)],
)
def test_bare_unallowlisted_open_fails(line: str, call: str) -> None:
    sites = scan_source(f"def f(p):\n    {line}\n", "m.py")
    assert [(s.call, s.shape) for s in sites] == [(call, "")]
    assert violations(sites, ()) == sites


def test_allowlisted_site_passes() -> None:
    sites = scan_source("class K:\n    def f(self, p):\n        return open_db(p)\n", "m.py")
    entry = Entry("m.py", "K.f", OPEN_DB, "factory", ("tests/x.py::test_x",))
    assert violations(sites, (entry,)) == []


# -- fixtures: allowlist self-checks ---------------------------------------------------------

_FACTORY = "def f(p):\n    return open_db(p)\n"


def _fixture_repo(tmp_path: Path, tests: Mapping[str, str]) -> tuple[Path, Path]:
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text(_FACTORY, encoding="utf-8")
    for name, body in tests.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return root, tmp_path


def test_fixture_repo_with_a_good_entry_passes(tmp_path: Path) -> None:
    root, repo = _fixture_repo(tmp_path, {"test_m.py": "def test_m():\n    pass\n"})
    entry = Entry("m.py", "f", OPEN_DB, "factory", ("test_m.py::test_m",))
    assert guard(root, repo, (entry,)) == []


def test_entry_whose_test_file_is_missing_fails(tmp_path: Path) -> None:
    root, repo = _fixture_repo(tmp_path, {})
    entry = Entry("m.py", "f", OPEN_DB, "factory", ("test_m.py::test_m",))
    assert guard(root, repo, (entry,)) == ["m.py f open_db: test file test_m.py does not exist"]


def test_entry_whose_node_id_is_missing_fails(tmp_path: Path) -> None:
    root, repo = _fixture_repo(tmp_path, {"test_m.py": "def test_other():\n    pass\n"})
    entry = Entry("m.py", "f", OPEN_DB, "factory", ("test_m.py::test_m",))
    assert guard(root, repo, (entry,)) == ["m.py f open_db: node id test_m.py::test_m does not exist"]


def test_stale_entry_fails(tmp_path: Path) -> None:
    root, repo = _fixture_repo(tmp_path, {"test_m.py": "def test_m():\n    pass\n"})
    good = Entry("m.py", "f", OPEN_DB, "factory", ("test_m.py::test_m",))
    stale = Entry("m.py", "gone", OPEN_DB, "factory", ("test_m.py::test_m",))
    assert guard(root, repo, (good, stale)) == ["m.py gone open_db: stale, matches no call"]
