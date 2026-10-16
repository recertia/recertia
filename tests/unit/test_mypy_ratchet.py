"""The mypy ratchet fails when a module's error count increases."""

from __future__ import annotations

from collections import Counter

from scripts.mypy_ratchet import compare_counts, parse_mypy_output


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
