"""Operator-only held-out eval runner that fails closed (REC-022).

The public golden split under ``evals/golden/`` stays the CI gate (``ci.yml`` runs ``pytest``).
The private held-out split never lives in this public repo. It runs only from
``.github/workflows/heldout-operator.yml`` (``workflow_dispatch``) or an operator shell.

Manifest (per the manifest spec). The public manifest ``evals/heldout/<split_id>.manifest.json`` holds
only ``schema_version, split_id, count, hash_rule ("sha256-sorted-relpath-v1"), root_sha256,
ciphertext_sha256, created_at`` (``created_at`` is RFC 3339 with an offset). This module has NO
manifest format of its own and does NOT hash the tree. The tree check is the WO-06
``verify_heldout_tree(manifest_path, root) -> HeldoutManifest`` (interface confirmed
by its author), imported by ONE from-import of ``verify_heldout_tree``,
``HeldoutManifestError`` and ``HeldoutManifest`` (the result type, for the isinstance
check). ``verify_heldout_tree`` is the ONLY WO-06 call: nothing here loads the manifest or
computes the root hash separately. A ``HeldoutManifestError`` is a fail-closed exit (2) that logs
main's hash/count-only message verbatim when it is one of WO-06's known messages
(``LOADER_MESSAGES`` / ``_LOADER_SHAPES``), else ``LOADER_REDACTED``.
Anything raised while importing the loader, or a missing name, fails closed with
``loader_unavailable`` (exit 2). The manifest ``count`` is WO-06's FILE count, checked by
``verify_heldout_tree`` itself. The one field used here before
the tree check is ``ciphertext_sha256`` (step 1 runs before decrypt); ``read_public_manifest``
refuses any key set other than the spec's seven.

Order. Every step fails closed before any eval runs:

0. Preflight (exit ``EXIT_REFUSED``). Refuses if debug logging is on, if the provider env is not
   empty, if any env selects a non-container backend or local exec, or (with
   ``--require-offline``) if a non-loopback interface exists.
1. Hash the ciphertext and compare it with ``ciphertext_sha256`` (``verify-ciphertext``, then
   again in ``run``).
2. Decrypt into ``$RUNNER_TEMP``. This is the workflow's TODO step; the key is pending approval.
3. Stage a copy of the decrypted tree under ``$TMPDIR`` (``$RUNNER_TEMP/tmp`` in the job;
   symlinks and special files are rejected), then call WO-06 ``verify_heldout_tree`` on the
   staged copy (root hash and file count); a tree with no task dirs fails closed. The split
   root, staging dir and audit store must all lie outside the checkout (cwd, repo root,
   ``$GITHUB_WORKSPACE``).
4. Run the container preflight (``solver/container.py`` ``ensure_execution_ready`` and
   ``probe_container_runtime``) against the digest-pinned local image. Digest pins run with
   ``--pull never`` and ``--network=none``.
5. Golden executor only (``use_skill_script=True``) on the staged copy. Constructing any
   ``ModelClient`` (``__new__`` or ``__init__``, every subclass) or importing a provider module
   exits 1, and so does any fixture whose commands used a backend other than ``container``.

Output rule. The console gets only counts, hashes, fixed reasons, and WO-06's known
hash/count-only messages, never names or paths. While the eval runs, fd 1/2 and sys.stdout/stderr go
to ``--audit-store``. Console lines are checked against a deny list built from the staged tree
(dir and file names, relpaths, skill ids, the first 40 chars of each request, the split-root
path); a hit exits ``EXIT_LEAK``. The ``scan`` subcommand re-checks captured console files and,
in the ``log-scan`` job, the published job log, with the same list.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# The WO-06 tree verifier (on main since #118, recertia.evals.heldout_manifest):
#   verify_heldout_tree(manifest_path: Path, root: Path) -> HeldoutManifest
#   raises HeldoutManifestError on any refusal or mismatch; messages carry hashes and counts only.
# The tree counts as verified ONLY if the result is a HeldoutManifest instance.
LOADER_MODULE = "recertia.evals.heldout_manifest"
HASH_RULE = "sha256-sorted-relpath-v1"

EXIT_OK = 0
EXIT_EVAL_FAILED = 1
EXIT_VERIFY_FAILED = 2
EXIT_USAGE = 3
EXIT_REFUSED = 4
EXIT_LEAK = 5

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20
_PUBLIC_EVALS = Path("evals")
_REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_KEYS = frozenset(
    {"schema_version", "split_id", "count", "hash_rule", "root_sha256", "ciphertext_sha256", "created_at"}
)
REQUEST_PREFIX_CHARS = 40
# RFC 3339 date-time with a numeric offset or Z.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
# WO-06's HeldoutManifestError messages (main, #118) carry hashes and counts only, never names.
# One is logged verbatim if it is one of these fixed messages or fully matches one
# of these shapes. Anything else (a name, a path, free text, a message WO-06 does not emit) replaces
# the WHOLE message with LOADER_REDACTED, so an id can never reach the public log through the
# loader. tests/unit/evals/test_heldout.py checks this list against main's module.
LOADER_MESSAGES = frozenset(
    {
        "manifest refused: duplicate key",
        "manifest refused: non-finite number",
        "manifest refused: symlink",
        "manifest refused: unreadable",
        "manifest refused: too large",
        "manifest refused: not UTF-8",
        "manifest refused: invalid JSON or trailing data",
        "manifest refused: top level must be an object",
        "manifest refused: schema_version must be the integer 1",
        "manifest refused: split_id format",
        "manifest refused: count must be an integer >= 1",
        f"manifest refused: hash_rule must be {HASH_RULE!r}",
        "manifest refused: root_sha256 must be 64 lowercase hex",
        "manifest refused: ciphertext_sha256 must be 64 lowercase hex",
        "manifest refused: created_at must be RFC 3339 with an explicit offset",
        "manifest refused: created_at is not a valid time",
        "manifest refused: created_at needs an offset",
        "held-out tree refused: non-UTF-8 name",
        "held-out tree refused: newline, CR or backslash in a name",
        "held-out tree refused: name is not NFC",
        "held-out tree refused: unreadable file",
        "held-out tree refused: special file",
        "held-out tree refused: unreadable directory",
        "held-out tree refused: empty directory",
        "held-out tree refused: symlink",
        "held-out tree refused: root is a symlink",
        "held-out tree refused: root is not a directory",
        "held-out tree refused: root is inside a git checkout (decrypt outside the repo)",
        "held-out tree refused: manifest lies inside the split root",
    }
)
_LOADER_SHAPES = (
    re.compile(r"manifest refused: \d{1,3} unknown and \d{1,3} missing key\(s\)"),
    re.compile(
        r"held-out (count|hash) mismatch: manifest root_sha256=[0-9a-f]{64} count=\d{1,12}; "
        r"tree root_sha256=[0-9a-f]{64} count=\d{1,12}"
    ),
)
LOADER_REDACTED = "manifest_error redacted"

# Executor: (staged fixture dir, task_class, audit run dir) -> passed.
Executor = Callable[[Path, str, Path], bool]


class Refused(Exception):
    """A fail-closed stop. ``reason`` is a fixed phrase with no ids or paths."""

    def __init__(self, reason: str, code: int = EXIT_VERIFY_FAILED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


# --------------------------------------------------------------------------- loader (WO-06)


def _loader() -> tuple[Callable[[Path, Path], Any], type[Exception], type]:
    """WO-06's verifier, error type and result type. Any import failure: fail closed, exit 2."""

    try:
        from recertia.evals.heldout_manifest import (  # type: ignore[import-not-found,unused-ignore]
            HeldoutManifest,
            HeldoutManifestError,
            verify_heldout_tree,
        )
    except Exception:  # noqa: BLE001 - ImportError or anything raised at import
        raise Refused("loader_unavailable") from None
    if (
        not callable(verify_heldout_tree)
        or not (isinstance(HeldoutManifestError, type) and issubclass(HeldoutManifestError, Exception))
        or not isinstance(HeldoutManifest, type)
    ):
        raise Refused("loader_unavailable")
    return verify_heldout_tree, HeldoutManifestError, HeldoutManifest


def loader_reason(exc: BaseException) -> str:
    """The HeldoutManifestError message if it is one of WO-06's known ones, else LOADER_REDACTED."""

    message = str(exc)
    if message in LOADER_MESSAGES or any(shape.fullmatch(message) for shape in _LOADER_SHAPES):
        return message
    return LOADER_REDACTED


def read_public_manifest(path: Path) -> dict[str, Any]:
    """The public manifest's fields, for step 1 only. Exactly the spec's seven keys.

    Not a manifest format: WO-06 ``verify_heldout_tree`` re-reads and validates the same file
    for the tree check. TODO(REC-022): use WO-06's public-manifest reader if it exports one.
    """

    _loader()  # no WO-06, no run: fail closed before touching the ciphertext
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("manifest invalid") from None
    if not isinstance(doc, dict) or set(doc) != MANIFEST_KEYS or doc.get("schema_version") != 1:
        raise Refused("manifest invalid")
    if doc.get("hash_rule") != HASH_RULE:
        raise Refused("manifest invalid")
    for name in ("root_sha256", "ciphertext_sha256"):
        if not isinstance(doc.get(name), str) or not _HEX64.match(doc[name]):
            raise Refused("manifest invalid")
    count = doc.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise Refused("manifest invalid")
    created = doc.get("created_at")
    if not isinstance(created, str) or not _RFC3339.match(created):
        raise Refused("manifest invalid")
    try:
        datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        raise Refused("manifest invalid") from None
    return doc


# --------------------------------------------------------------------------- verification


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_ciphertext(manifest: Mapping[str, Any], ciphertext: Path) -> None:
    """Step 1: the ciphertext file hash (a single opaque blob, not the tree rule)."""

    try:
        st = os.lstat(ciphertext)
    except OSError:
        raise Refused("hash mismatch stage=ciphertext") from None
    if not stat.S_ISREG(st.st_mode) or _sha256_file(ciphertext) != manifest["ciphertext_sha256"]:
        raise Refused("hash mismatch stage=ciphertext")


def _inside(path: Path, parent: Path) -> bool:
    return path.resolve().is_relative_to(parent.resolve())


def stage_tree(split_root: Path, staging: Path) -> None:
    """Copy regular files and dirs only. Symlinks and special files fail closed."""

    if not split_root.is_dir() or split_root.is_symlink():
        raise Refused("hash mismatch stage=root")
    for dirpath, dirnames, filenames in os.walk(split_root, followlinks=False):
        src_dir = Path(dirpath)
        dest_dir = staging / src_dir.relative_to(split_root)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in dirnames + filenames:
            st = os.lstat(src_dir / name)
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise Refused("hash mismatch stage=root")
            shutil.copyfile(src_dir / name, dest_dir / name, follow_symlinks=False)


def task_dirs(root: Path) -> list[Path]:
    """Fixture dirs, ``<root>/<task_class>/<task>/`` holding ``task.json`` or ``goal.json``."""

    found = []
    for cls in sorted(p for p in root.iterdir() if p.is_dir()):
        for task in sorted(p for p in cls.iterdir() if p.is_dir()):
            if (task / "task.json").is_file() or (task / "goal.json").is_file():
                found.append(task)
    return found


def verify_tree(manifest_path: Path, manifest: Mapping[str, Any], staged: Path) -> list[Path]:
    """Step 3: WO-06 ``verify_heldout_tree`` over the staged copy (the only WO-06 call).

    ``HeldoutManifestError`` fails closed and logs WO-06's hash/count message (``loader_reason``).
    Any other exception, or a result that is not a ``HeldoutManifest`` instance, fails closed with
    a fixed phrase. The manifest ``count`` is a FILE count and WO-06 has already compared it with
    the tree, so it is not compared with the task dirs here. A tree with no task
    dirs fails closed.
    """

    verify_heldout_tree, heldout_manifest_error, heldout_manifest = _loader()
    try:
        result = verify_heldout_tree(manifest_path, staged)
    except heldout_manifest_error as exc:
        raise Refused(loader_reason(exc)) from None
    except Exception:  # noqa: BLE001 - outside the WO-06 contract: still fail closed
        raise Refused("verifier_error") from None
    # Only a real HeldoutManifest counts as verified (not 0, "", {"ok": False}, None, ...).
    if not isinstance(result, heldout_manifest):
        raise Refused("verifier_error")
    tasks = task_dirs(staged)
    if not tasks:
        raise Refused("count mismatch tasks=0")
    return tasks


# --------------------------------------------------------------------------- guards


_ABSENT = object()


class ModelClientForbidden(RuntimeError):
    """A model client was constructed during a held-out run."""


PROVIDER_MODULES = ("recertia.solver.providers", "anthropic", "openai")


def _all_subclasses(cls: type) -> list[type]:
    found: list[type] = []
    stack: list[type] = list(cls.__subclasses__())
    while stack:
        sub = stack.pop()
        found.append(sub)
        stack.extend(sub.__subclasses__())
    return found


class _ProviderImportBlocker:
    """``sys.meta_path`` finder: importing a provider module during a held-out run is refused."""

    def __init__(self, constructed: list[str]) -> None:
        self.constructed = constructed

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> None:
        del path, target
        if any(name == mod or name.startswith(mod + ".") for mod in PROVIDER_MODULES):
            self.constructed.append(f"import:{name.split('.')[0]}")
            raise ModelClientForbidden("provider modules are forbidden in held-out runs")
        return None


@contextlib.contextmanager
def forbid_model_clients(constructed: list[str]) -> Iterator[None]:
    """Refuse every ``ModelClient`` construction and every provider-module import; record each.

    - ``__init__`` is replaced on ``ModelClient`` AND on every subclass that defines its own, so a
      subclass that skips ``super().__init__`` is still refused. A
      subclass created during the run is caught by a temporary ``__init_subclass__``. (Patching
      ``__new__`` is avoided: CPython keeps a stale ``tp_new`` slot after it is removed again.)
    - A ``sys.meta_path`` finder refuses importing ``PROVIDER_MODULES`` (the guard is in place
      at provider-module import). Provider modules already in ``sys.modules`` are hidden for the
      duration, so a lazy ``from recertia.solver.providers import ...`` re-imports and is refused.
    - The patches are on class objects and the import system, so lazy imports that run later
      (``nodes/plan.py:108``'s ``guide_stitch``) see them. The record survives an engine that
      catches the error, so ``run`` still exits 1.
    """

    from recertia.solver import model

    base = model.ModelClient

    def _refuse_init(self: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        constructed.append(type(self).__name__)
        raise ModelClientForbidden("model clients are forbidden in held-out runs")

    saved: list[tuple[type, str, Any]] = []

    def _guard(cls: type) -> None:
        if "__init__" in cls.__dict__ or cls is base:
            saved.append((cls, "__init__", cls.__dict__.get("__init__", _ABSENT)))
            setattr(cls, "__init__", _refuse_init)

    def _on_subclass(cls: type, **kwargs: Any) -> None:
        super(base, cls).__init_subclass__(**kwargs)  # type: ignore[misc]
        _guard(cls)

    for cls in [base, *_all_subclasses(base)]:
        _guard(cls)
    saved.append((base, "__init_subclass__", base.__dict__.get("__init_subclass__", _ABSENT)))
    setattr(base, "__init_subclass__", classmethod(_on_subclass))
    hidden = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if any(name == mod or name.startswith(mod + ".") for mod in PROVIDER_MODULES)
    }
    blocker = _ProviderImportBlocker(constructed)
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(blocker)
        for cls, name, value in reversed(saved):
            if value is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(cls, name)
            else:
                setattr(cls, name, value)
        sys.modules.update(hidden)


@contextlib.contextmanager
def track_backends(used: list[str]) -> Iterator[None]:
    """Record the backend of every command, and refuse every local run.

    ``_container_run`` and ``_local_run`` (``solver/container.py``) are the only process
    launchers. ``solver/executor.py`` imports both by name, so both modules are patched.
    """

    from recertia.solver import container, executor
    from recertia.solver.sandbox import SandboxError

    real_container = container._container_run

    def _container(*args: Any, **kwargs: Any) -> Any:
        used.append("container")
        return real_container(*args, **kwargs)

    def _local(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        used.append("local")
        raise SandboxError("held-out runs refuse the local backend")

    saved = [(m, n, getattr(m, n)) for m in (container, executor) for n in ("_container_run", "_local_run")]
    for mod in (container, executor):
        setattr(mod, "_container_run", _container)
        setattr(mod, "_local_run", _local)
    try:
        yield
    finally:
        for mod, name, value in saved:
            setattr(mod, name, value)


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEBUG_VARS = ("RUNNER_DEBUG", "ACTIONS_STEP_DEBUG", "ACTIONS_RUNNER_DEBUG")
_PROVIDER_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_BACKEND_VARS = ("RECERTIA_EXECUTION_BACKEND", "RECERTIA_GOLDEN_BACKEND")
_LOCAL_EXEC_VARS = ("RECERTIA_API_ALLOW_LOCAL_EXEC",)
_NET_DEV = Path("/proc/self/net/dev")


def debug_logging_on(env: Mapping[str, str]) -> bool:
    return any(env.get(name, "").strip().lower() in _TRUTHY for name in _DEBUG_VARS)


def network_reachable(net_dev: Path = _NET_DEV) -> bool:
    """True unless loopback is this process's only interface (a fresh ``unshare --net``).

    It reads ``/proc/self/net/dev``, which reflects the caller's network namespace. It opens no
    socket (tests/boundary/test_network_egress_boundary.py). If the table can't be read, it
    fails closed.
    """

    try:
        lines = net_dev.read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return True
    names = {line.split(":", 1)[0].strip() for line in lines if ":" in line}
    return names != {"lo"}


def checkout_roots(env: Mapping[str, str]) -> list[Path]:
    """The checkout as seen by the job: cwd, this repo's root, and ``$GITHUB_WORKSPACE``."""

    roots = [Path.cwd(), _REPO_ROOT]
    if env.get("GITHUB_WORKSPACE"):
        roots.append(Path(env["GITHUB_WORKSPACE"]))
    return roots


def location_refusal(env: Mapping[str, str], *, split_root: Path, audit_store: Path) -> str | None:
    """Decrypted bytes, their staged copy and the audit store stay out of the checkout.

    Staging goes under ``tempfile.gettempdir()`` (``$TMPDIR``). With ``$RUNNER_TEMP`` set, staging
    and the split root must also be inside it, so the ``if: always()`` wipe covers them.
    """

    staging_parent = Path(tempfile.gettempdir())
    for path in (split_root, staging_parent, audit_store):
        if any(_inside(path, root) for root in checkout_roots(env)):
            return "path_in_checkout"
    runner_temp = env.get("RUNNER_TEMP")
    if runner_temp and not all(_inside(p, Path(runner_temp)) for p in (split_root, staging_parent)):
        return "path_outside_runner_temp"
    return None


def preflight(env: Mapping[str, str], *, require_offline: bool) -> str | None:
    """Return a refusal reason, or ``None``. Runs before the manifest is read."""

    if debug_logging_on(env):
        return "debug_logging_on"
    if any(env.get(name, "") for name in _PROVIDER_VARS):
        return "provider_env_not_empty"
    if any(env.get(name, "container").strip().lower() != "container" for name in _BACKEND_VARS):
        return "backend_not_container"
    if any(env.get(name, "").strip().lower() in _TRUTHY for name in _LOCAL_EXEC_VARS):
        return "backend_not_container"
    if env.get("RECERTIA_CONTAINER_RUNTIME", "docker").strip().lower() != "docker":
        return "runtime_not_docker"
    if require_offline and network_reachable():
        return "network_reachable"
    return None


def container_preflight() -> None:
    """The container preflight (``solver/container.py:101,176``) against the local image.

    It requires the container backend for both the run and the golden surface, and a
    ``@sha256:`` image, so that ``_container_run`` passes ``--pull never`` (a missing image
    then fails instead of pulling). ``ensure_execution_ready`` checks the runtime, and
    ``probe_container_runtime`` runs ``true`` in a ``--network=none`` container. Any failure
    raises.
    """

    from recertia.settings import load_settings
    from recertia.solver.container import (
        configured_backend,
        default_container_image,
        ensure_execution_ready,
        probe_container_runtime,
    )
    from recertia.solver.sandbox import SandboxError

    settings = load_settings()
    if configured_backend() != "container" or settings.resolved_golden_backend() != "container":
        raise SandboxError("held-out runs require the container backend")
    if "@sha256:" not in default_container_image():
        raise SandboxError("held-out runs require a digest-pinned image")
    # Docker only. Rootless podman needs setuid newuidmap, which --no-new-privs blocks.
    if settings.container_runtime != "docker":
        raise SandboxError("held-out runs require RECERTIA_CONTAINER_RUNTIME=docker")
    if ensure_execution_ready() != "container":
        raise SandboxError("execution backend is not container")
    probe_container_runtime(timeout_s=60)


# --------------------------------------------------------------------------- executor


def _skill_id_for(fixture: Path) -> str:
    task = fixture / "task.json"
    if task.is_file():
        spec = json.loads(task.read_text(encoding="utf-8"))
        if isinstance(spec, dict) and isinstance(spec.get("expected_skill_id"), str):
            return str(spec["expected_skill_id"])
    return fixture.name


def golden_executor(fixture: Path, task_class: str, run_dir: Path) -> bool:
    """Golden executor only: the skill's own script (``use_skill_script=True``).

    No proposer, no LLM plan or review stage, no model key. A fixture with no matching skill
    version fails; it is not routed to ``run_goal_fixture``.
    """

    from recertia.evals.golden import run_golden_for_skill
    from recertia.evals.store import EvalStore
    from recertia.memory.procedural.seeds import SEED_SKILLS
    from recertia.memory.procedural.store import SkillStore
    from recertia.settings import load_settings

    if load_settings().resolved_golden_backend() != "container":
        raise RuntimeError("golden backend is not container")
    skill_id = _skill_id_for(fixture)
    loaded = [v for v, _st, _stats in SkillStore(Path("skills")).iter_loaded()] + list(SEED_SKILLS)
    versions = [v for v in loaded if v.skill_id == skill_id and v.task_class == task_class]
    if not versions:
        return False
    store = EvalStore(run_dir / "evals.db")
    try:
        result = run_golden_for_skill(
            versions[0],
            fixture,
            runs_root=run_dir / "runs",
            use_skill_script=True,
            snapshot_id="heldout",
            eval_store=store,
        )
    finally:
        store.close()
    return bool(result.passed)


# --------------------------------------------------------------------------- output


_DENY_PATTERNS = (
    re.compile(r"golden-[A-Za-z0-9._-]+-v[0-9]+-[0-9a-f]{6}"),
    re.compile(r"golden-[A-Za-z0-9._-]+-[0-9a-f]{6}"),
    re.compile(r"failing_fixtures|golden_path"),
    re.compile(r"task\.json|goal\.json|expect\.json"),
)


def _request_prefix(fixture: Path) -> str | None:
    task = fixture / "task.json"
    try:
        spec = json.loads(task.read_text(encoding="utf-8")) if task.is_file() else None
    except ValueError:
        return None
    if isinstance(spec, dict) and isinstance(spec.get("request"), str):
        prefix = spec["request"].strip()[:REQUEST_PREFIX_CHARS].rstrip()
        return prefix if len(prefix) >= 8 else None
    return None


def deny_terms(root: Path, extra: Sequence[str] = ()) -> list[str]:
    """Proposal §4 item 11: every dir and file name and relpath under ``root``, each skill id,
    the first 40 chars of each request, plus ``extra`` (the split-root path)."""

    terms: set[str] = set(extra)
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        terms.add(rel.as_posix())
        terms.update(rel.parts)
    for task in task_dirs(root) if root.is_dir() else []:
        terms.add(_skill_id_for(task))
        prefix = _request_prefix(task)
        if prefix:
            terms.add(prefix)
    return sorted((t for t in terms if t), key=len, reverse=True)


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


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------- entry points


def run(
    *,
    manifest_path: Path,
    ciphertext: Path,
    split_root: Path,
    audit_store: Path,
    verify_only: bool = False,
    require_offline: bool = False,
    executor: Executor | None = None,
    preflight_containers: Callable[[], None] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    env = env if env is not None else os.environ
    deny: list[str] = []
    leaks: list[int] = []

    def say(line: str) -> None:
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
    if _PUBLIC_EVALS.exists() and _inside(split_root, _PUBLIC_EVALS):
        say("FAIL reason=split_root_in_repo executed=0")
        return EXIT_REFUSED
    location = location_refusal(env, split_root=split_root, audit_store=audit_store)
    if location is not None:
        say(f"FAIL reason={location} executed=0")
        return EXIT_REFUSED

    with contextlib.ExitStack() as stack:
        try:
            audit_store.mkdir(parents=True, exist_ok=True)
            run_dir = _new_run_dir(audit_store)
        except OSError:
            say("FAIL reason=audit_store_unwritable")
            return EXIT_VERIFY_FAILED
        staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="heldout-")))
        record: dict[str, Any] = {"verify_only": verify_only}
        try:
            manifest = read_public_manifest(manifest_path)  # public fields only
            verify_ciphertext(manifest, ciphertext)  # step 1
            stage_tree(split_root, staging)  # step 2 output, staged
            tasks = verify_tree(manifest_path, manifest, staging)  # step 3 (WO-06)
        except Refused as exc:
            record.update(status="verify_failed", reason=exc.reason)
            _write_json(run_dir / "record.json", record)
            say(f"FAIL {exc.reason} executed=0")
            return exc.code
        except OSError:
            record.update(status="verify_failed", reason="io_error")
            _write_json(run_dir / "record.json", record)
            say("FAIL reason=io_error executed=0")
            return EXIT_VERIFY_FAILED

        deny = deny_terms(staging, [str(split_root), str(staging)])
        root_sha = manifest["root_sha256"]
        say(f"VERIFIED root_sha256={root_sha} count={manifest['count']} tasks={len(tasks)}")
        record.update(root_sha256=root_sha, count=manifest["count"], tasks=len(tasks))
        if verify_only:
            record.update(status="verified")
            _write_json(run_dir / "record.json", record)
            say("PASS verify-only executed=0")
            return EXIT_LEAK if leaks else EXIT_OK

        execute = executor or golden_executor
        check_containers = preflight_containers or container_preflight
        constructed: list[str] = []
        results: list[dict[str, Any]] = []
        stage_failed: str | None = None
        with (run_dir / "eval.log").open("w", encoding="utf-8") as log:
            os.chmod(run_dir / "eval.log", 0o600)
            # Both guards go in before the container preflight and before any node can run.
            with _capture_fds(log), forbid_model_clients(constructed):
                preflight_used: list[str] = []
                try:
                    with track_backends(preflight_used):
                        check_containers()
                except Exception as exc:  # noqa: BLE001 - detail only in the audit log
                    print(f"container preflight failed: {type(exc).__name__}: {exc}", file=log)
                    stage_failed = "container_preflight"
                for index, task in enumerate([] if stage_failed else tasks):
                    used: list[str] = []
                    passed = False
                    error = None
                    try:
                        with track_backends(used):
                            passed = execute(task, task.parent.name, run_dir)
                    except Exception as exc:  # noqa: BLE001
                        error = type(exc).__name__
                    backends = sorted(set(used)) or ["none"]
                    results.append(
                        {
                            "index": index,
                            "fixture": task.name,
                            "passed": bool(passed) and error is None,
                            "backend_used": backends,
                            "error": error,
                        }
                    )
        _write_json(run_dir / "results.json", results)
        if stage_failed:
            record.update(status=stage_failed)
            _write_json(run_dir / "record.json", record)
            say(f"FAIL reason={stage_failed} executed=0")
            return EXIT_REFUSED
        if constructed:
            record.update(status="model_client_constructed", model_clients=constructed)
            _write_json(run_dir / "record.json", record)
            say(f"FAIL reason=model_client_constructed count={len(constructed)}")
            return EXIT_EVAL_FAILED
        off_backend = sum(1 for r in results if r["backend_used"] != ["container"])
        if off_backend:
            record.update(status="backend_mismatch")
            _write_json(run_dir / "record.json", record)
            say(f"FAIL reason=backend_not_container runs={off_backend}")
            return EXIT_EVAL_FAILED
        passed_n = sum(1 for r in results if r["passed"])
        failed_n = len(results) - passed_n
        record.update(status="evaluated", passed=passed_n, failed=failed_n)
        _write_json(run_dir / "record.json", record)
        verdict = "PASS" if failed_n == 0 and passed_n > 0 else "FAIL"
        say(f"{verdict} passed={passed_n} failed={failed_n}")
        if leaks:
            return EXIT_LEAK
        return EXIT_OK if verdict == "PASS" else EXIT_EVAL_FAILED


def scan(*, split_root: Path, logs: Sequence[Path], out: TextIO, public: Sequence[Path] = ()) -> int:
    """Post-run scan of captured console files, or of the published job log, for deny terms.

    The terms are the same as the in-run list (names, relpaths, skill ids, request prefixes,
    the split-root path). ``public`` files hold text that is already public (the workflow file,
    and the step ``env:`` values GitHub echoes into every published log). A term found in that
    text carries no private information, so it is dropped instead of failing every scan. Only
    counts are printed.
    """

    if not split_root.is_dir():
        print("log-scan: FAIL reason=split_root_missing", file=out)
        return EXIT_VERIFY_FAILED
    terms = deny_terms(split_root, [str(split_root)])
    try:
        public_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in public)
    except OSError:
        print("log-scan: FAIL reason=public_text_missing", file=out)
        return EXIT_VERIFY_FAILED
    if public_text:
        terms = [t for t in terms if t not in public_text]
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


def _parse(parser: argparse.ArgumentParser, argv: Sequence[str]) -> argparse.Namespace | None:
    try:
        return parser.parse_args(argv)
    except SystemExit:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    command = args_list[0] if args_list and args_list[0] in ("scan", "verify-ciphertext") else "run"
    rest = args_list[1:] if command != "run" else args_list
    parser = _QuietParser(prog=f"python -m recertia.evals.heldout {command}")
    if command == "scan":
        parser.add_argument("--split-root", type=Path, required=True)
        parser.add_argument("--log", type=Path, action="append", required=True)
        parser.add_argument("--public-text", type=Path, action="append", default=[])
        args = _parse(parser, rest)
        if args is None:
            return EXIT_USAGE
        return scan(split_root=args.split_root, logs=args.log, out=sys.stdout, public=args.public_text)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ciphertext", type=Path, required=True)
    if command == "verify-ciphertext":
        args = _parse(parser, rest)
        if args is None:
            return EXIT_USAGE
        try:
            verify_ciphertext(read_public_manifest(args.manifest), args.ciphertext)
        except Refused as exc:
            print(f"heldout: FAIL {exc.reason} executed=0")
            return exc.code
        print("heldout: VERIFIED ciphertext")
        return EXIT_OK
    parser.add_argument("--split-root", type=Path, required=True)
    parser.add_argument("--audit-store", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--require-offline", action="store_true")
    args = _parse(parser, rest)
    if args is None:
        return EXIT_USAGE
    return run(
        manifest_path=args.manifest,
        ciphertext=args.ciphertext,
        split_root=args.split_root,
        audit_store=args.audit_store,
        verify_only=args.verify_only,
        require_offline=args.require_offline,
    )


if __name__ == "__main__":
    raise SystemExit(main())
