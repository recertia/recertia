#!/usr/bin/env python3
"""Fail when mypy error fingerprint counts differ from ``mypy-baseline.txt``.

Each mypy error is keyed by a fingerprint: ``(path, error code, message)``.
Backslashes in the path become ``/``, so a Windows run gives the same keys as
CI. mypy's own line references in the message (``on line 12``, ``at line 12``)
become ``on line N`` and ``at line N``; other text such as ``Literal['line 1']``
is kept. Line numbers are not part of the key, so moving an existing error to
another line still passes. The baseline holds a count per fingerprint. Any
fingerprint whose count rises fails, so fixing one error cannot hide a new one
in the same file, whether the new error has the same code or a different one.
A fingerprint missing from the baseline is treated as zero.

The ratchet is exact. A fingerprint whose count drops below the baseline also
fails ("baseline is stale"), so a fix lowers the baseline in the same commit
(``python3 scripts/mypy_ratchet.py --write``) and leaves no slack for a later
error to spend.

The baseline header records the mypy version that wrote it
(``# mypy-version: X``). The check runs first: a different running mypy fails
with a version-mismatch message, because error counts and messages change
between mypy releases. A baseline file without the header fails too.

``tests`` is a ratchet target only. It is not in ``[tool.mypy] files`` in
pyproject.toml, so the plain ``mypy`` CI step does not check it; its existing
errors are seeded in the same baseline and new ones fail here.

``strict_modules`` is the per-module promotion list. It is empty until a
later change opts a zero-baseline module into ``mypy --strict``. Those modules
run in a separate ``mypy --strict`` invocation that must report no errors, so
``--strict`` never leaks into the baselined run.

The end-to-end tests copy this script into a temporary repo layout; the
script has no hidden flags.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_NAME = "mypy-baseline.txt"
BASELINE_PATH = REPO_ROOT / BASELINE_NAME
TARGETS = ["contracts", "src/recertia", "tests"]

# Modules already at zero that a later PR promotes to --strict.
strict_modules: list[str] = []

_ERROR_RE = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?: error: (?P<msg>.*?)(?:  \[(?P<code>[a-z0-9-]+)\])?$")
_LINE_REF_RE = re.compile(r"\b(?P<prep>on|at) line \d+\b")
_VERSION_HEADER_RE = re.compile(r"^#\s*mypy-version:\s*(?P<version>\S+)\s*$")
_MYPY_VERSION_RE = re.compile(r"^mypy (?P<version>\S+)")
RIPPLE_HINT = "hint: if you changed types in contracts/ or src/, review the message diff and run --write"


class Fingerprint(NamedTuple):
    path: str
    code: str
    message: str


def normalise_message(message: str) -> str:
    return _LINE_REF_RE.sub(r"\g<prep> line N", message.strip())


def normalise_path(path: str) -> str:
    """``tests\\unit\\a.py`` (mypy on Windows) and ``tests/unit/a.py`` are one key."""

    return path.replace("\\", "/")


def parse_mypy_output(text: str) -> Counter[Fingerprint]:
    counts: Counter[Fingerprint] = Counter()
    for line in text.splitlines():
        match = _ERROR_RE.match(line)
        if match:
            key = Fingerprint(
                normalise_path(match.group("path")),
                match.group("code") or "",
                normalise_message(match.group("msg")),
            )
            counts[key] += 1
    return counts


def read_baseline(path: Path) -> Counter[Fingerprint]:
    """Parse ``count<TAB>path<TAB>code<TAB>message`` lines; ``#`` starts a comment line."""

    counts: Counter[Fingerprint] = Counter()
    if not path.exists():
        return counts
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        count, file_path, code, message = raw.split("\t", 3)
        counts[Fingerprint(normalise_path(file_path), code, message)] += int(count)
    return counts


def read_baseline_version(path: Path) -> str | None:
    """The ``# mypy-version: X`` header value, or None when the header is absent."""

    for raw in path.read_text(encoding="utf-8").splitlines():
        match = _VERSION_HEADER_RE.match(raw.strip())
        if match:
            return match.group("version")
    return None


def check_version(path: Path, running: str) -> str | None:
    """A failure message when the baseline was not written by the running mypy.

    A missing baseline file has no version to check (it means zero errors). An
    existing file without the header is rejected: every committed baseline
    carries one, and accepting headerless files would let deleting one line
    switch the check off.
    """

    if not path.exists():
        return None
    recorded = read_baseline_version(path)
    if recorded is None:
        return (
            f"{path.name} has no '# mypy-version:' header; regenerate it with the pinned mypy "
            f"(running mypy {running}): python3 scripts/mypy_ratchet.py --write"
        )
    if recorded != running:
        return (
            f"mypy version mismatch: baseline written with mypy {recorded}, running mypy {running}. "
            f"Install mypy=={recorded} (the pin in pyproject.toml and .github/workflows/ci.yml); "
            "to upgrade mypy instead, change both pins and regenerate the baseline in the same commit."
        )
    return None


def render_baseline(counts: Counter[Fingerprint], mypy_version: str) -> str:
    lines = [
        f"# mypy-version: {mypy_version}",
        "# count<TAB>path<TAB>error code<TAB>message ('on line N', 'at line N' normalised).",
        "# Generated by scripts/mypy_ratchet.py --write. Counts must match exactly:",
        "# a rise fails, and a fix fails as stale until --write lowers it here.",
        "# A missing fingerprint means zero.",
        "",
    ]
    for key in sorted(counts):
        if counts[key] > 0:
            lines.append(f"{counts[key]}\t{key.path}\t{key.code}\t{key.message}")
    lines.append("")
    return "\n".join(lines)


def write_baseline(path: Path, text: str) -> None:
    """Write ``text`` with the existing file's line endings (CRLF or LF), else LF.

    Pinning the newline keeps ``--write`` output the same on every platform.
    """

    newline = "\r\n" if path.exists() and b"\r\n" in path.read_bytes() else "\n"
    with path.open("w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)


def _describe(key: Fingerprint) -> str:
    return f"{key.path} [{key.code or 'no code'}] {key.message}"


def compare_counts(actual: Counter[Fingerprint], baseline: Counter[Fingerprint]) -> list[str]:
    """Return human-readable failures. Empty means the ratchet holds."""

    failures: list[str] = []
    stale: list[str] = []
    fixed = 0
    new = False
    for key in sorted(set(actual) | set(baseline)):
        now = actual.get(key, 0)
        was = baseline.get(key, 0)
        if now > was:
            new = True
            if key not in baseline:
                failures.append(f"new mypy error ({now}x): {_describe(key)}")
            else:
                failures.append(f"{was} -> {now}: {_describe(key)}")
        elif now < was:
            fixed += was - now
            stale.append(f"{was} -> {now} (fixed, baseline not lowered): {_describe(key)}")
    if fixed:
        failures.extend(stale)
        failures.append(
            f"baseline is stale: {fixed} fixed, run python3 scripts/mypy_ratchet.py --write and commit"
        )
        if new:
            # New and stale together is the signature of a message ripple (N6).
            failures.append(RIPPLE_HINT)
    return failures


def mypy_env() -> dict[str, str]:
    """The caller's environment without PYTHONPATH.

    CI sets no PYTHONPATH. A local PYTHONPATH that holds the repo root makes mypy
    treat ``scripts`` (reached from tests/) as an installed package and hide its
    errors, so local runs and ``--write`` would disagree with CI.
    """

    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def _mypy_base() -> list[str]:
    return [sys.executable, "-m", "mypy", "--no-error-summary", f"--cache-dir={os.devnull}"]


def mypy_command() -> list[str]:
    """The mypy command line, run without a cache.

    Messages are part of the fingerprint, and mypy 2.4.0 renders some of them
    differently when a module comes from a cache that another invocation wrote
    (CI's plain ``mypy contracts/ src/recertia/`` step runs just before this).
    A cold run gives the same text every time.
    """

    return [*_mypy_base(), *TARGETS]


def strict_command(modules: list[str]) -> list[str]:
    """``mypy --strict`` over the promoted modules only, separate from the baselined run."""

    return [*_mypy_base(), "--strict", *modules]


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=mypy_env())
    # mypy exits 1 when it finds errors. That is an input to the ratchet, not a crash.
    if proc.returncode not in {0, 1}:
        sys.stderr.write(proc.stderr or proc.stdout)
        raise SystemExit(proc.returncode)
    return proc.stdout


def run_mypy() -> Counter[Fingerprint]:
    return parse_mypy_output(_run(mypy_command()))


def run_strict() -> list[str]:
    """Errors from the strict modules; any error fails, whatever the baseline says."""

    if not strict_modules:
        return []
    errors = parse_mypy_output(_run(strict_command(strict_modules)))
    return [f"strict module error ({n}x): {_describe(key)}" for key, n in sorted(errors.items())]


def running_mypy_version() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=mypy_env(),
        check=False,
    )
    match = _MYPY_VERSION_RE.match(proc.stdout.strip())
    if proc.returncode != 0 or not match:
        sys.stderr.write(proc.stderr or proc.stdout or "mypy --version failed\n")
        raise SystemExit(2)
    return match.group("version")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the baseline")
    args = parser.parse_args(argv)
    version = running_mypy_version()
    if args.write:
        write_baseline(BASELINE_PATH, render_baseline(run_mypy(), version))
        print(f"wrote {BASELINE_PATH} (mypy {version})")
        return 0
    mismatch = check_version(BASELINE_PATH, version)
    if mismatch:
        print(f"mypy ratchet failed: {mismatch}", file=sys.stderr)
        return 1
    actual = run_mypy()
    failures = compare_counts(actual, read_baseline(BASELINE_PATH)) + run_strict()
    if failures:
        print("mypy ratchet failed:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    total = sum(actual.values())
    print(f"mypy ratchet ok ({total} errors, {len(actual)} fingerprints, mypy {version}, baseline holds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
