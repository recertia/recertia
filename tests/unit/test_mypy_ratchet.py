"""The mypy ratchet fails when a module's error count increases."""

from __future__ import annotations

import tomllib
from collections import Counter

from scripts.mypy_ratchet import REPO_ROOT, TARGETS, compare_counts, parse_mypy_output


def test_ratchet_fails_when_a_module_count_increases() -> None:
    baseline = {"src/recertia/foo.py": 2}
    actual = Counter({"src/recertia/foo.py": 3})
    failures = compare_counts(actual, baseline)
    assert failures == ["src/recertia/foo.py: 2 -> 3 mypy errors"]


def test_ratchet_fails_on_a_new_module() -> None:
    failures = compare_counts(Counter({"src/recertia/new.py": 1}), {})
    assert any("new module" in line for line in failures)


def test_ratchet_allows_a_lower_count() -> None:
    assert compare_counts(Counter({"src/recertia/foo.py": 1}), {"src/recertia/foo.py": 4}) == []


def test_parse_mypy_output_counts_per_file() -> None:
    text = "\n".join(
        [
            "src/recertia/a.py:10: error: boom",
            "src/recertia/a.py:11: error: boom",
            "src/recertia/b.py:1: note: not an error",
            "contracts/c.py:3: error: nope",
        ]
    )
    assert parse_mypy_output(text) == Counter({"src/recertia/a.py": 2, "contracts/c.py": 1})


def test_ratchet_targets_tests_but_plain_mypy_files_do_not() -> None:
    assert "tests" in TARGETS
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "tests" not in pyproject["tool"]["mypy"]["files"]


def test_ratchet_fails_on_a_new_error_in_a_tests_file() -> None:
    text = "\n".join(
        [
            "tests/unit/test_a.py:5: error: boom  [arg-type]",
            "tests/unit/test_a.py:9: error: boom  [arg-type]",
            "tests/unit/test_b.py:1: error: new  [assignment]",
        ]
    )
    baseline = {"tests/unit/test_a.py": 1}
    assert compare_counts(parse_mypy_output(text), baseline) == [
        "tests/unit/test_a.py: 1 -> 2 mypy errors",
        "tests/unit/test_b.py: new module with 1 mypy error(s)",
    ]
