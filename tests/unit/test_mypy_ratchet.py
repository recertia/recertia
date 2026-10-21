"""The mypy ratchet fails when a fingerprint count rises, on stale entries and on a mypy version mismatch."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

import pytest

from scripts.mypy_ratchet import (
    REPO_ROOT,
    RIPPLE_HINT,
    TARGETS,
    Fingerprint,
    check_version,
    compare_counts,
    mypy_command,
    mypy_env,
    normalise_message,
    parse_mypy_output,
    read_baseline,
    read_baseline_version,
    render_baseline,
    running_mypy_version,
    strict_command,
    write_baseline,
)

SCRIPT = REPO_ROOT / "scripts" / "mypy_ratchet.py"
VERSION = "2.4.0"
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


def test_ratchet_fails_as_stale_when_a_count_drops() -> None:
    failures = compare_counts(Counter({ASSIGN: 1}), Counter({ASSIGN: 3}))
    assert failures == [
        f"3 -> 1 (fixed, baseline not lowered): tests/unit/test_a.py [assignment] {ASSIGN.message}",
        "baseline is stale: 2 fixed, run python3 scripts/mypy_ratchet.py --write and commit",
    ]


def test_new_plus_stale_adds_the_ripple_hint() -> None:
    other = ASSIGN._replace(message=ASSIGN.message.replace('"int")', '"float")'))
    failures = compare_counts(Counter({other: 1}), Counter({ASSIGN: 1}))
    assert failures[-1] == RIPPLE_HINT
    assert RIPPLE_HINT not in compare_counts(Counter({ASSIGN: 2}), Counter({ASSIGN: 1}))
    assert RIPPLE_HINT not in compare_counts(Counter(), Counter({ASSIGN: 1}))


def test_ratchet_holds_on_an_exact_match() -> None:
    assert compare_counts(Counter({ASSIGN: 2}), Counter({ASSIGN: 2})) == []


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


def test_windows_path_gives_the_same_key_as_a_posix_path() -> None:
    posix = parse_mypy_output("tests/foo/bar.py:3: error: boom  [misc]")
    windows = parse_mypy_output("tests\\foo\\bar.py:3: error: boom  [misc]")
    assert windows == posix == Counter({Fingerprint("tests/foo/bar.py", "misc", "boom"): 1})


def test_windows_path_in_the_baseline_reads_as_a_posix_key(tmp_path: Path) -> None:
    path = tmp_path / "baseline.txt"
    path.write_text("# mypy-version: 2.4.0\n1\ttests\\foo\\bar.py\tmisc\tboom\n", encoding="utf-8")
    assert read_baseline(path) == Counter({Fingerprint("tests/foo/bar.py", "misc", "boom"): 1})
    assert (
        compare_counts(parse_mypy_output("tests/foo/bar.py:9: error: boom  [misc]"), read_baseline(path))
        == []
    )


def test_only_mypy_line_references_are_normalised() -> None:
    assert normalise_message('Name "x" already defined on line 3') == 'Name "x" already defined on line N'
    assert normalise_message("defined at line 12") == "defined at line N"
    literal = "Incompatible types in assignment (expression has type \"Literal['line 2']\")"
    assert normalise_message(literal) == literal


def test_baseline_round_trips(tmp_path: Path) -> None:
    counts = Counter({ASSIGN: 2, Fingerprint("contracts/c.py", "", "odd [x] message: y"): 1})
    path = tmp_path / "baseline.txt"
    path.write_text(render_baseline(counts, VERSION), encoding="utf-8")
    assert read_baseline(path) == counts


@pytest.mark.parametrize("existing", [b"# old\r\n", b"# old\n", None])
def test_write_baseline_keeps_existing_line_endings(tmp_path: Path, existing: bytes | None) -> None:
    path = tmp_path / "baseline.txt"
    if existing is not None:
        path.write_bytes(existing)
    write_baseline(path, render_baseline(Counter({ASSIGN: 2}), VERSION))
    data = path.read_bytes()
    if existing == b"# old\r\n":
        assert data.count(b"\r\n") == data.count(b"\n") > 0
    else:
        assert b"\r" not in data
    assert read_baseline(path) == Counter({ASSIGN: 2})


def test_committed_baseline_parses() -> None:
    baseline = read_baseline(REPO_ROOT / "mypy-baseline.txt")
    assert baseline and all(count > 0 for count in baseline.values())
    assert all("\\" not in key.path for key in baseline)


def test_render_baseline_writes_the_version_header() -> None:
    text = render_baseline(Counter({ASSIGN: 1}), "9.9.9")
    assert text.splitlines()[0] == "# mypy-version: 9.9.9"


def test_version_mismatch_names_both_versions(tmp_path: Path) -> None:
    path = tmp_path / "mypy-baseline.txt"
    write_baseline(path, render_baseline(Counter({ASSIGN: 1}), "1.11.2"))
    assert read_baseline_version(path) == "1.11.2"
    message = check_version(path, "2.4.0")
    assert message is not None
    assert message.startswith("mypy version mismatch: baseline written with mypy 1.11.2, running mypy 2.4.0.")
    assert "stale" not in message and "--write" not in message
    assert check_version(path, "1.11.2") is None


def test_headerless_baseline_is_rejected_and_a_missing_one_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "mypy-baseline.txt"
    assert check_version(path, VERSION) is None
    path.write_text(f"1\t{ASSIGN.path}\t{ASSIGN.code}\t{ASSIGN.message}\n", encoding="utf-8")
    message = check_version(path, VERSION)
    assert message is not None and "no '# mypy-version:' header" in message


def test_pins_and_baseline_header_agree_on_one_mypy_version() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    pins = re.findall(r"pip install mypy==(\S+)", ci)
    assert pins == [VERSION]
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = [req for req in pyproject["project"]["optional-dependencies"]["dev"] if req.startswith("mypy")]
    assert dev == [f"mypy>={VERSION},<={VERSION}"]
    assert read_baseline_version(REPO_ROOT / "mypy-baseline.txt") == VERSION


def test_running_mypy_matches_the_pin() -> None:
    assert running_mypy_version() == VERSION


def test_ratchet_targets_tests_but_plain_mypy_files_do_not() -> None:
    assert "tests" in TARGETS
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "tests" not in pyproject["tool"]["mypy"]["files"]


def test_ci_pins_mypy_and_keeps_the_plain_gating_step() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install mypy==2.4.0" in ci
    assert "run: mypy contracts/ src/recertia/\n" in ci
    assert "run: python3 scripts/mypy_ratchet.py\n" in ci


def test_mypy_subprocess_env_drops_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    env = mypy_env()
    assert "PYTHONPATH" not in env
    assert env["PATH"] == os.environ["PATH"]


def test_mypy_runs_without_a_cache() -> None:
    cmd = mypy_command()
    assert f"--cache-dir={os.devnull}" in cmd
    assert cmd[-len(TARGETS) :] == TARGETS


def test_strict_modules_run_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.mypy_ratchet.strict_modules", ["tests/strict_mod.py"])
    assert "--strict" not in mypy_command()
    cmd = strict_command(["tests/strict_mod.py"])
    assert cmd[-2:] == ["--strict", "tests/strict_mod.py"]
    assert not set(TARGETS) & set(cmd)


def test_script_has_no_hidden_flags() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--root" not in source and "--target" not in source


# End to end: copy the real script into a temporary repo layout and run it (and real mypy)
# against a module in that layout's tests/.

BASE_MODULE = """\
x: int = "a"


def f(n: int) -> int:
    return n
"""


def _layout(tmp_path: Path, strict: list[str] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    script = SCRIPT.read_text(encoding="utf-8")
    if strict is not None:
        line = "strict_modules: list[str] = []"
        assert line in script
        script = script.replace(line, f"strict_modules: list[str] = {strict!r}")
    (root / "scripts" / "mypy_ratchet.py").write_text(script, encoding="utf-8")
    for package in ("contracts", "src/recertia"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    return root


def _ratchet(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "mypy_ratchet.py"), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(tmp_path: Path) -> Path:
    root = _layout(tmp_path)
    module = root / "tests" / "mod.py"
    module.write_text(BASE_MODULE, encoding="utf-8")
    seeded = _ratchet(root, "--write")
    assert seeded.returncode == 0, seeded.stderr
    assert _ratchet(root).returncode == 0
    return module


def _root(module: Path) -> Path:
    return module.parents[1]


def test_e2e_same_code_swap_fails(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    # Fix the [assignment] error and add a different [assignment] error in the same file.
    module.write_text(BASE_MODULE.replace('x: int = "a"', "x: int = 1\ny: str = 2"), encoding="utf-8")
    result = _ratchet(_root(module))
    assert result.returncode == 1
    assert "new mypy error (1x): tests/mod.py [assignment]" in result.stderr
    assert 'expression has type "int", variable has type "str"' in result.stderr


def test_e2e_different_code_swap_fails(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    # Fix the [assignment] error and add an [arg-type] error in the same file.
    module.write_text(BASE_MODULE.replace('x: int = "a"', "x: int = 1") + 'f("no")\n', encoding="utf-8")
    result = _ratchet(_root(module))
    assert result.returncode == 1
    assert "new mypy error (1x): tests/mod.py [arg-type]" in result.stderr


def test_e2e_line_move_passes(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    module.write_text("\n" * 40 + BASE_MODULE, encoding="utf-8")
    result = _ratchet(_root(module))
    assert result.returncode == 0, result.stderr
    assert "mypy ratchet ok (1 errors, 1 fingerprints" in result.stdout


def test_e2e_fix_without_lowering_the_baseline_fails_as_stale(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    module.write_text(BASE_MODULE.replace('x: int = "a"', "x: int = 1"), encoding="utf-8")
    result = _ratchet(_root(module))
    assert result.returncode == 1
    assert "baseline is stale: 1 fixed, run python3 scripts/mypy_ratchet.py --write" in result.stderr


def test_e2e_fix_plus_write_passes(tmp_path: Path) -> None:
    module = _seed(tmp_path)
    module.write_text(BASE_MODULE.replace('x: int = "a"', "x: int = 1"), encoding="utf-8")
    assert _ratchet(_root(module), "--write").returncode == 0
    result = _ratchet(_root(module))
    assert result.returncode == 0, result.stderr
    assert "mypy ratchet ok (0 errors, 0 fingerprints" in result.stdout


def test_e2e_write_keeps_crlf_and_the_header(tmp_path: Path) -> None:
    root = _root(_seed(tmp_path))
    baseline = root / "mypy-baseline.txt"
    baseline.write_bytes(baseline.read_bytes().replace(b"\n", b"\r\n"))
    assert _ratchet(root, "--write").returncode == 0
    data = baseline.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") > 0
    assert data.startswith(f"# mypy-version: {VERSION}\r\n".encode())
    assert _ratchet(root).returncode == 0


def test_e2e_different_mypy_version_fails_with_a_version_mismatch(tmp_path: Path) -> None:
    root = _root(_seed(tmp_path))
    baseline = root / "mypy-baseline.txt"
    header = f"# mypy-version: {VERSION}"
    text = baseline.read_text(encoding="utf-8")
    assert header in text
    baseline.write_text(text.replace(header, "# mypy-version: 1.11.2"), encoding="utf-8")
    result = _ratchet(root)
    assert result.returncode == 1
    assert (
        f"mypy version mismatch: baseline written with mypy 1.11.2, running mypy {VERSION}." in result.stderr
    )
    assert "stale" not in result.stderr and "--write" not in result.stderr


def test_e2e_headerless_baseline_fails(tmp_path: Path) -> None:
    root = _root(_seed(tmp_path))
    baseline = root / "mypy-baseline.txt"
    lines = baseline.read_text(encoding="utf-8").splitlines(keepends=True)
    baseline.write_text("".join(line for line in lines if "mypy-version" not in line), encoding="utf-8")
    result = _ratchet(root)
    assert result.returncode == 1
    assert "no '# mypy-version:' header" in result.stderr


def test_e2e_windows_paths_in_the_baseline_still_hold(tmp_path: Path) -> None:
    root = _root(_seed(tmp_path))
    baseline = root / "mypy-baseline.txt"
    text = baseline.read_text(encoding="utf-8")
    assert "\ttests/mod.py\t" in text
    baseline.write_text(text.replace("\ttests/mod.py\t", "\ttests\\mod.py\t"), encoding="utf-8")
    result = _ratchet(root)
    assert result.returncode == 0, result.stderr


def test_e2e_strict_module_errors_fail_and_strict_does_not_leak(tmp_path: Path) -> None:
    root = _layout(tmp_path, strict=["tests/strict_mod.py"])
    untyped = "def g(x):\n    return x\n"
    (root / "tests" / "strict_mod.py").write_text(untyped, encoding="utf-8")
    (root / "tests" / "loose.py").write_text(untyped.replace("g(", "h("), encoding="utf-8")
    # No baseline (zero errors): a leaked --strict would report loose.py as a new error.
    result = _ratchet(root)
    assert result.returncode == 1
    assert "strict module error (1x): tests/strict_mod.py [no-untyped-def]" in result.stderr
    assert "loose.py" not in result.stderr
