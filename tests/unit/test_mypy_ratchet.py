"""The mypy ratchet fails when a mypy error fingerprint's count rises."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

from scripts.mypy_ratchet import (
    REPO_ROOT,
    TARGETS,
    Fingerprint,
    compare_counts,
    parse_mypy_output,
    read_baseline,
    render_baseline,
)

SCRIPT = REPO_ROOT / "scripts" / "mypy_ratchet.py"
ASSIGN = Fingerprint(
    "tests/unit/test_a.py",
    "assignment",
    'Incompatible types in assignment (expression has type "str", variable has type "int")',
)


def test_ratchet_fails_when_a_fingerprint_count_increases() -> None:
    failures = compare_counts(Counter({ASSIGN: 3}), Counter({ASSIGN: 2}))
    assert failures == [f"2 -> 3: tests/unit/test_a.py [assignment] {ASSIGN.message}"]


def test_ratchet_fails_on_a_new_fingerprint() -> None:
    failures = compare_counts(Counter({ASSIGN: 1}), Counter())
    assert failures == [f"new mypy error (1x): tests/unit/test_a.py [assignment] {ASSIGN.message}"]


def test_parse_mypy_output_keys_by_path_code_and_message_without_line_numbers() -> None:
    text = "\n".join(
        [
            "src/recertia/a.py:10: error: boom  [misc]",
            "src/recertia/a.py:11: error: boom  [misc]",
            'src/recertia/a.py:12: error: Name "x" already defined on line 3  [no-redef]',
            'src/recertia/a.py:40: error: Name "x" already defined on line 30  [no-redef]',
            "src/recertia/b.py:1: note: not an error",
            "contracts/c.py:3:7: error: no code here",
        ]
    )
    assert parse_mypy_output(text) == Counter(
        {
            Fingerprint("src/recertia/a.py", "misc", "boom"): 2,
            Fingerprint("src/recertia/a.py", "no-redef", 'Name "x" already defined on line N'): 2,
            Fingerprint("contracts/c.py", "", "no code here"): 1,
        }
    )


def test_baseline_round_trips(tmp_path: Path) -> None:
    counts = Counter({ASSIGN: 2, Fingerprint("contracts/c.py", "", "odd [x] message: y"): 1})
    path = tmp_path / "baseline.txt"
    path.write_text(render_baseline(counts), encoding="utf-8")
    assert read_baseline(path) == counts


def test_committed_baseline_parses() -> None:
    baseline = read_baseline(REPO_ROOT / "mypy-baseline.txt")
    assert baseline and all(count > 0 for count in baseline.values())


def test_ratchet_targets_tests_but_plain_mypy_files_do_not() -> None:
    assert "tests" in TARGETS
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "tests" not in pyproject["tool"]["mypy"]["files"]


# End to end: run the real script (and real mypy) against a temporary module.

BASE_MODULE = """\
x: int = "a"


def f(n: int) -> int:
    return n
"""


def _ratchet(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--target", "mod.py", *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(tmp_path: Path) -> Path:
    module = tmp_path / "mod.py"
    module.write_text(BASE_MODULE, encoding="utf-8")
    seeded = _ratchet(tmp_path, "--write")
    assert seeded.returncode == 0, seeded.stderr
    assert _ratchet(tmp_path).returncode == 0
    return module


def test_e2e_same_code_swap_fails(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    # Fix the [assignment] error and add a different [assignment] error in the same file.
    module.write_text(BASE_MODULE.replace('x: int = "a"', "x: int = 1\ny: str = 2"), encoding="utf-8")
    result = _ratchet(tmp_path)
    assert result.returncode == 1
    assert "new mypy error (1x): mod.py [assignment]" in result.stderr
    assert 'expression has type "int", variable has type "str"' in result.stderr


def test_e2e_different_code_swap_fails(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    # Fix the [assignment] error and add an [arg-type] error in the same file.
    module.write_text(BASE_MODULE.replace('x: int = "a"', "x: int = 1") + 'f("no")\n', encoding="utf-8")
    result = _ratchet(tmp_path)
    assert result.returncode == 1
    assert "new mypy error (1x): mod.py [arg-type]" in result.stderr


def test_e2e_line_move_passes(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    module.write_text("\n" * 40 + BASE_MODULE, encoding="utf-8")
    result = _ratchet(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "mypy ratchet ok (1 errors, 1 fingerprints" in result.stdout
