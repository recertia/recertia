"""Operator-only held-out eval runner that fails closed on any hash mismatch (REC-022).

The public golden split under ``evals/golden/`` stays the CI gate (``.github/workflows/ci.yml``
runs ``pytest``). The private held-out split never lives in this public repo. It runs only
from ``.github/workflows/heldout-eval.yml`` (``workflow_dispatch``) or an operator shell.

A pinned manifest is a JSON file::

    {
      "schema": "recertia.heldout-manifest/v1",
      "task_class": "repo-chore",
      "files": [{"path": "<fixture>/task.json", "sha256": "<64 hex>"}, ...]
    }

``path`` is relative to ``--split-root``. The first path segment is the fixture directory.

Preflight (exit ``EXIT_REFUSED`` before the manifest is read): debug logging is off
(``RUNNER_DEBUG`` / ``ACTIONS_STEP_DEBUG`` / ``ACTIONS_RUNNER_DEBUG``), the provider key env is
empty, and with ``--require-offline`` the process sees a network interface other than loopback.

Fail closed. Before anything runs, this module checks, in order:

1. ``--expected-sha256`` is 64 lowercase hex characters.
2. The sha256 of the manifest bytes equals ``--expected-sha256``.
3. The manifest parses, names the v1 schema, and lists at least one file. Every path is
   relative, has no ``..``, and is unique.
4. Every listed file exists under ``--split-root`` as a regular file (not a symlink), and its
   bytes hash to the listed sha256.
5. The split root holds no files that the manifest does not list.

Verified bytes are copied into a private staging directory while they are hashed. The eval
then reads the staged copy, not the split root, so a file swapped after the check is never run.
Any failure exits ``EXIT_VERIFY_FAILED`` and the executor is never called.

While the eval runs, constructing any model client (``recertia.solver.model.ModelClient``)
is refused and makes the run exit 1 (WO-08 Amendment 1: no model key, golden executor only).

Output rule. stdout and stderr carry only counts, hashes, and PASS/FAIL. Fixture ids, paths,
and contents never go to stdout or stderr, because a workflow run's logs are public. Per-fixture
results and failure details go only to ``--audit-store``, the operator-only audit store. While
the eval runs, fd 1/2 and sys.stdout/stderr are redirected into that store so engine logs cannot
leak ids. Every console line is checked against a deny list (fixture dirs, paths, skill ids,
run_id shapes) and replaced by ``FAIL reason=log_scan`` on a hit (exit ``EXIT_LEAK``). The
``scan`` subcommand runs the same check over captured console files after the run.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

MANIFEST_SCHEMA = "recertia.heldout-manifest/v1"

EXIT_OK = 0
EXIT_EVAL_FAILED = 1
EXIT_VERIFY_FAILED = 2
EXIT_USAGE = 3
EXIT_REFUSED = 4
EXIT_LEAK = 5

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20

# Executor: (staged fixture dirs, task_class, audit run dir) -> (passed, failed).
Executor = Callable[[list[Path], str, Path], tuple[int, int]]


class VerificationError(Exception):
    """The pinned inputs did not verify. ``reason`` is a fixed code with no ids or paths."""

    def __init__(self, reason: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass
class Verified:
    manifest_sha256: str
    task_class: str
    file_count: int
    fixture_count: int
    inputs_digest: str
    staged_fixtures: list[Path] = field(default_factory=list)
    entries: list[tuple[PurePosixPath, str]] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_rel(raw: object) -> PurePosixPath | None:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        return None
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        return None
    if len(rel.parts) < 2:
        return None  # files must sit inside a fixture directory
    return rel


def _parse_manifest(data: bytes) -> tuple[str, list[tuple[PurePosixPath, str]]]:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError("manifest_unparseable") from None
    if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA:
        raise VerificationError("manifest_schema")
    task_class = doc.get("task_class")
    if not isinstance(task_class, str) or not task_class:
        raise VerificationError("manifest_task_class")
    files = doc.get("files")
    if not isinstance(files, list) or not files:
        raise VerificationError("manifest_no_files")
    entries: list[tuple[PurePosixPath, str]] = []
    seen: set[PurePosixPath] = set()
    bad = 0
    for item in files:
        rel = _safe_rel(item.get("path")) if isinstance(item, dict) else None
        digest = item.get("sha256") if isinstance(item, dict) else None
        if rel is None or not isinstance(digest, str) or not _HEX64.match(digest) or rel in seen:
            bad += 1
            continue
        seen.add(rel)
        entries.append((rel, digest))
    if bad:
        raise VerificationError("manifest_bad_entries", detail={"bad_entries": bad})
    return task_class, entries


def verify_and_stage(
    manifest_path: Path, expected_sha256: str, split_root: Path, staging: Path
) -> Verified:
    """Verify every pinned hash and copy verified bytes to ``staging``. Raises on any failure."""

    expected = expected_sha256.strip().lower()
    if not _HEX64.match(expected):
        raise VerificationError("expected_sha256_malformed")
    if not manifest_path.is_file():
        raise VerificationError("manifest_missing")
    data = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(data).hexdigest()
    if manifest_sha != expected:
        raise VerificationError("manifest_hash_mismatch", detail={"manifest_sha256": manifest_sha})
    task_class, entries = _parse_manifest(data)
    if not split_root.is_dir():
        raise VerificationError("split_root_missing")

    root = split_root.resolve()
    missing: list[str] = []
    mismatched: list[str] = []
    for rel, want in entries:
        src = split_root.joinpath(*rel.parts)
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            missing.append(str(rel))
            continue
        if not stat.S_ISREG(st.st_mode) or not src.resolve().is_relative_to(root):
            mismatched.append(str(rel))
            continue
        dest = staging.joinpath(*rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with src.open("rb") as fin, dest.open("wb") as fout:
            for chunk in iter(lambda: fin.read(_CHUNK), b""):
                digest.update(chunk)
                fout.write(chunk)
        if digest.hexdigest() != want:
            mismatched.append(str(rel))

    listed = {str(rel) for rel, _ in entries}
    unlisted = sorted(
        p.relative_to(split_root).as_posix()
        for p in split_root.rglob("*")
        if (p.is_file() or p.is_symlink()) and p.relative_to(split_root).as_posix() not in listed
        and p.resolve() != manifest_path.resolve()
    )
    if missing or mismatched or unlisted:
        reason = (
            "input_missing" if missing else "input_hash_mismatch" if mismatched else "input_unlisted"
        )
        raise VerificationError(
            reason,
            detail={"missing": missing, "mismatched": mismatched, "unlisted": unlisted},
        )

    inputs_digest = hashlib.sha256(
        "".join(f"{rel.as_posix()}\0{want}\n" for rel, want in sorted(entries)).encode()
    ).hexdigest()
    fixtures = sorted({rel.parts[0] for rel, _ in entries})
    return Verified(
        manifest_sha256=manifest_sha,
        task_class=task_class,
        file_count=len(entries),
        fixture_count=len(fixtures),
        inputs_digest=inputs_digest,
        staged_fixtures=[staging / name for name in fixtures],
        entries=entries,
    )


class ModelClientForbidden(RuntimeError):
    """A model client was constructed during a held-out run (WO-08 Amendment 1: no model key)."""


@contextlib.contextmanager
def forbid_model_clients(constructed: list[str]) -> Iterator[None]:
    """Make every ``ModelClient`` constructor raise, and record each attempt in ``constructed``.

    All model clients subclass ``recertia.solver.model.ModelClient`` and call its ``__init__``
    (``StubModelClient``, ``AnthropicModelClient``, ``OpenAIModelClient``). Patching the base
    ``__init__`` therefore covers every client. The record survives even if the engine catches
    the exception, so ``run`` still exits 1.
    """

    from recertia.solver import model

    original = model.ModelClient.__init__

    def _refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        constructed.append(type(self).__name__)
        raise ModelClientForbidden("model clients are forbidden in held-out runs")

    model.ModelClient.__init__ = _refuse  # type: ignore[method-assign]
    try:
        yield
    finally:
        model.ModelClient.__init__ = original  # type: ignore[method-assign]


def _skill_id_for(fixture: Path) -> str:
    """``task.json`` ``expected_skill_id`` (supports opaque dir names), else the dir name."""

    task = fixture / "task.json"
    if task.is_file():
        spec = json.loads(task.read_text(encoding="utf-8"))
        if isinstance(spec, dict) and isinstance(spec.get("expected_skill_id"), str):
            return str(spec["expected_skill_id"])
    return fixture.name


def golden_executor(fixtures: list[Path], task_class: str, run_dir: Path) -> tuple[int, int]:
    """Golden executor only: each fixture runs its skill's own script (``use_skill_script=True``).

    No proposer, no LLM plan or review stage, no model key. A fixture with no matching skill
    version (a goal-only fixture would need the proposer path) counts as failed; it is not
    routed to ``run_goal_fixture``.
    """

    from recertia.evals.golden import run_golden_for_skill
    from recertia.evals.store import EvalStore
    from recertia.memory.procedural.seeds import SEED_SKILLS
    from recertia.memory.procedural.store import SkillStore

    store = EvalStore(run_dir / "evals.db")
    skills = SkillStore(Path("skills"))
    loaded = [v for v, _st, _stats in skills.iter_loaded()] + list(SEED_SKILLS)
    results: list[dict[str, Any]] = []
    try:
        for fixture in fixtures:
            skill_id = _skill_id_for(fixture)
            versions = [v for v in loaded if v.skill_id == skill_id and v.task_class == task_class]
            if not versions:
                results.append({"fixture": fixture.name, "passed": False, "detail": "no_skill_version"})
                continue
            r = run_golden_for_skill(
                versions[0],
                fixture,
                runs_root=run_dir / "runs",
                use_skill_script=True,
                snapshot_id="heldout",
                eval_store=store,
            )
            results.append(
                {"fixture": fixture.name, "passed": r.passed, "terminal": r.terminal, "detail": r.detail}
            )
    finally:
        store.close()
    (run_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    passed = sum(1 for r in results if r["passed"])
    return passed, len(results) - passed


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEBUG_VARS = ("RUNNER_DEBUG", "ACTIONS_STEP_DEBUG", "ACTIONS_RUNNER_DEBUG")
_PROVIDER_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_NET_DEV = Path("/proc/self/net/dev")


def debug_logging_on(env: Mapping[str, str]) -> bool:
    return any(env.get(name, "").strip().lower() in _TRUTHY for name in _DEBUG_VARS)


def network_reachable(net_dev: Path = _NET_DEV) -> bool:
    """True unless loopback is this process's only interface (a fresh ``unshare --net``).

    Reads ``/proc/self/net/dev``, which reflects the caller's network namespace (``/sys/class/net``
    does not without a remount). Opens no socket (tests/boundary/test_network_egress_boundary.py).
    Fails closed: if the interface table cannot be read, the network counts as reachable.
    """

    try:
        lines = net_dev.read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return True
    names = {line.split(":", 1)[0].strip() for line in lines if ":" in line}
    return names != {"lo"}


def preflight(env: Mapping[str, str], *, require_offline: bool) -> str | None:
    """Return a refusal reason, or ``None``. Runs before the manifest is even read."""

    if debug_logging_on(env):
        return "debug_logging_on"
    if any(env.get(name, "") for name in _PROVIDER_VARS):
        return "provider_env_not_empty"
    if require_offline and network_reachable():
        return "network_reachable"
    return None


# Shapes that carry fixture ids in this repo's eval output (golden.py run_id and report fields).
_DENY_PATTERNS = (
    re.compile(r"golden-[A-Za-z0-9._-]+-v[0-9]+-[0-9a-f]{6}"),
    re.compile(r"golden-[A-Za-z0-9._-]+-[0-9a-f]{6}"),
    re.compile(r"failing_fixtures|golden_path"),
    re.compile(r"task\.json|goal\.json|expect\.json"),
)


def deny_terms(entries: Sequence[tuple[PurePosixPath, str]], extra: Sequence[str] = ()) -> list[str]:
    terms = {rel.parts[0] for rel, _ in entries} | {rel.as_posix() for rel, _ in entries}
    terms |= {t for t in extra if t}
    return sorted(terms, key=len, reverse=True)


def count_leaks(text: str, terms: Sequence[str]) -> int:
    hits = sum(text.count(term) for term in terms)
    return hits + sum(len(p.findall(text)) for p in _DENY_PATTERNS)


@contextlib.contextmanager
def _capture_fds(log: TextIO) -> Iterator[None]:
    """Send fd 1/2 (subprocesses, C extensions) and sys.stdout/stderr to ``log``."""

    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    try:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            yield
    finally:
        log.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in saved:
            os.close(fd)


def _new_run_dir(audit_store: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = audit_store / f"heldout-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    return run_dir


def _write_record(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "record.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def run(
    *,
    manifest_path: Path,
    expected_sha256: str,
    split_root: Path,
    audit_store: Path,
    verify_only: bool = False,
    require_offline: bool = False,
    executor: Executor | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    env = env if env is not None else os.environ
    deny: list[str] = []
    leaks: list[int] = []

    def say(line: str) -> None:
        # In-job log-scan guard: nothing reaches the public console if it matches the deny list.
        text = f"heldout: {line}"
        hits = count_leaks(text, deny)
        if hits:
            text = f"heldout: FAIL reason=log_scan hits={hits}"
            leaks.append(hits)
        print(text, file=out, flush=True)

    refusal = preflight(env, require_offline=require_offline)
    if refusal is not None:
        say(f"FAIL reason={refusal} executed=0")
        return EXIT_REFUSED

    with contextlib.ExitStack() as stack:
        try:
            audit_store.mkdir(parents=True, exist_ok=True)
            run_dir = _new_run_dir(audit_store)
        except OSError:
            say("FAIL reason=audit_store_unwritable")
            return EXIT_VERIFY_FAILED
        staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="heldout-")))
        record: dict[str, Any] = {"schema": MANIFEST_SCHEMA, "verify_only": verify_only}
        try:
            verified = verify_and_stage(manifest_path, expected_sha256, split_root, staging)
        except VerificationError as exc:
            record.update(status="verify_failed", reason=exc.reason, detail=exc.detail)
            _write_record(run_dir, record)
            counts = {k: len(v) for k, v in exc.detail.items() if isinstance(v, list)}
            extra = "".join(f" {k}={n}" for k, n in sorted(counts.items()))
            say(f"FAIL reason={exc.reason}{extra} executed=0")
            return EXIT_VERIFY_FAILED
        except OSError:
            record.update(status="verify_failed", reason="io_error")
            _write_record(run_dir, record)
            say("FAIL reason=io_error executed=0")
            return EXIT_VERIFY_FAILED

        deny = deny_terms(
            verified.entries, [_skill_id_for(f) for f in verified.staged_fixtures if f.is_dir()]
        )
        say(
            f"VERIFIED manifest_sha256={verified.manifest_sha256} inputs_sha256={verified.inputs_digest} "
            f"files={verified.file_count} fixtures={verified.fixture_count}"
        )
        record.update(
            manifest_sha256=verified.manifest_sha256,
            inputs_sha256=verified.inputs_digest,
            files=verified.file_count,
            fixtures=verified.fixture_count,
        )
        if verify_only:
            record.update(status="verified")
            _write_record(run_dir, record)
            say("PASS verify-only executed=0")
            return EXIT_LEAK if leaks else EXIT_OK

        execute = executor or golden_executor
        constructed: list[str] = []
        log_path = run_dir / "eval.log"
        error: str | None = None
        passed = failed = 0
        with log_path.open("w", encoding="utf-8") as log:
            try:
                with _capture_fds(log), forbid_model_clients(constructed):
                    passed, failed = execute(verified.staged_fixtures, verified.task_class, run_dir)
            except Exception as exc:  # noqa: BLE001 - detail goes only to the audit store
                error = type(exc).__name__
        if constructed:
            record.update(status="model_client_constructed", model_clients=constructed)
            _write_record(run_dir, record)
            say(f"FAIL reason=model_client_constructed count={len(constructed)}")
            return EXIT_EVAL_FAILED
        if error is not None:
            record.update(status="eval_error", error_type=error)
            _write_record(run_dir, record)
            say("FAIL reason=eval_error")
            return EXIT_EVAL_FAILED
        record.update(status="evaluated", passed=passed, failed=failed)
        _write_record(run_dir, record)
        verdict = "PASS" if failed == 0 and passed > 0 else "FAIL"
        say(f"{verdict} passed={passed} failed={failed}")
        if leaks:
            return EXIT_LEAK
        return EXIT_OK if verdict == "PASS" else EXIT_EVAL_FAILED


def scan(*, manifest_path: Path, expected_sha256: str, logs: Sequence[Path], out: TextIO) -> int:
    """Post-run scan of captured console output against the verified manifest's ids."""

    expected = expected_sha256.strip().lower()
    try:
        data = manifest_path.read_bytes()
    except OSError:
        print("log-scan: FAIL reason=manifest_missing", file=out)
        return EXIT_VERIFY_FAILED
    if not _HEX64.match(expected) or hashlib.sha256(data).hexdigest() != expected:
        print("log-scan: FAIL reason=manifest_hash_mismatch", file=out)
        return EXIT_VERIFY_FAILED
    try:
        _task_class, entries = _parse_manifest(data)
    except VerificationError as exc:
        print(f"log-scan: FAIL reason={exc.reason}", file=out)
        return EXIT_VERIFY_FAILED
    terms = deny_terms(entries)
    hits = 0
    for log in logs:
        try:
            hits += count_leaks(log.read_text(encoding="utf-8", errors="replace"), terms)
        except OSError:
            print("log-scan: FAIL reason=log_missing", file=out)
            return EXIT_VERIFY_FAILED
    if hits:
        print(f"log-scan: FAIL hits={hits}", file=out)
        return EXIT_LEAK
    print(f"log-scan: PASS hits=0 logs={len(logs)}", file=out)
    return EXIT_OK


class _QuietParser(argparse.ArgumentParser):
    """Usage errors print a fixed line; argparse would echo argument values (paths) to stderr."""

    def error(self, message: str) -> Any:
        del message
        print("heldout: FAIL reason=usage executed=0", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["scan"]:
        sp = _QuietParser(prog="python -m recertia.evals.heldout scan")
        sp.add_argument("--manifest", type=Path, required=True)
        sp.add_argument("--expected-sha256", required=True)
        sp.add_argument("--log", type=Path, action="append", required=True)
        try:
            sargs = sp.parse_args(args_list[1:])
        except SystemExit as exc:
            return EXIT_USAGE if exc.code else EXIT_OK
        return scan(
            manifest_path=sargs.manifest,
            expected_sha256=sargs.expected_sha256,
            logs=sargs.log,
            out=sys.stdout,
        )
    parser = _QuietParser(
        prog="python -m recertia.evals.heldout",
        description="Operator-only held-out eval runner; fails closed on any hash mismatch.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--split-root", type=Path, required=True)
    parser.add_argument("--audit-store", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--require-offline", action="store_true")
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    return run(
        manifest_path=args.manifest,
        expected_sha256=args.expected_sha256,
        split_root=args.split_root,
        audit_store=args.audit_store,
        verify_only=args.verify_only,
        require_offline=args.require_offline,
    )


if __name__ == "__main__":
    raise SystemExit(main())
