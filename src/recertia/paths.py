"""Path containment helpers — refuse escapes outside an allowed root."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class PathEscapeError(ValueError):
    """Raised when a resolved path escapes its allowed root."""


class HostRootError(ValueError):
    """Raised when a registered host root is invalid."""


def contained_path(root: Path | str, *parts: str) -> Path:
    """Join ``parts`` under ``root`` and require the result stay inside ``root``.

    Symlink components are resolved before the containment check.
    """

    base = Path(root).resolve()
    candidate = base.joinpath(*parts).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise PathEscapeError(f"path {candidate} escapes root {base}") from exc
    return candidate


def is_within(root: Path | str, path: Path | str) -> bool:
    """Return True when ``path`` resolves inside ``root``."""

    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def validate_workspace_id(workspace_id: str) -> str:
    if not _WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise ValueError(
            "workspace_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ "
            "(no path separators or traversal)"
        )
    return workspace_id


def looks_absolute(path: str) -> bool:
    """True for OS-absolute paths and Windows drive-letter / UNC forms."""

    s = path.strip()
    if not s:
        return False
    if Path(s).is_absolute():
        return True
    if _WINDOWS_DRIVE_RE.match(s):
        return True
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    return False


def split_rel_subpath(subpath: str | None) -> tuple[str, ...]:
    """Split a relative subpath on ``/`` and ``\\``; empty / ``.`` → ()."""

    if subpath is None:
        return ()
    raw = subpath.strip()
    if raw in {"", "."}:
        return ()
    if looks_absolute(raw):
        raise HostRootError("workdir must be relative to the registered workspace (absolute paths rejected)")
    parts = [p for p in re.split(r"[\\/]+", raw) if p and p != "."]
    if any(p == ".." for p in parts):
        # Still allow join+resolve containment to catch encoded forms; explicit .. fails early.
        pass
    return tuple(parts)


def _posix_roots_allowed() -> bool:
    from recertia.settings import load_settings

    return load_settings().allow_posix_workspace_roots


def normalize_host_root(host_root: str, *, must_exist: bool = True) -> str:
    """Validate and normalize a registered host root for storage.

    Primary profile: Windows drive-letter absolute paths (``D:\\src\\repo``).
    When ``RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS=1`` (CI / non-Windows tests), POSIX
    absolute directories are also accepted.
    """

    raw = host_root.strip().rstrip("/\\")
    if not raw or "\x00" in raw:
        raise HostRootError("host_root must be a non-empty directory path")

    if raw.startswith("\\\\") or raw.startswith("//") or raw.startswith("\\\\?\\"):
        raise HostRootError("UNC and extended-length paths are not supported")

    is_windows_form = bool(_WINDOWS_DRIVE_RE.match(raw))
    if is_windows_form:
        if sys.platform == "win32":
            path = Path(raw)
            if not path.is_absolute():
                raise HostRootError(
                    "host_root must be a Windows drive-letter absolute directory"
                )
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise HostRootError(f"host_root could not be resolved: {exc}") from exc
            if must_exist and not resolved.is_dir():
                raise HostRootError("host_root must exist as a directory")
            # Prefer backslash storage on Windows.
            return str(resolved)
        # Non-Windows cannot resolve D:\… — reject unless callers use POSIX escape.
        raise HostRootError(
            "Windows host_root cannot be resolved on this host; "
            "run the API on Windows or set RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS=1 "
            "with a POSIX absolute host_root for tests"
        )

    if _posix_roots_allowed():
        path = Path(raw)
        if not path.is_absolute():
            raise HostRootError(
                "host_root must be a Windows drive-letter absolute directory "
                "(or POSIX absolute when RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS=1)"
            )
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise HostRootError(f"host_root could not be resolved: {exc}") from exc
        if must_exist and not resolved.is_dir():
            raise HostRootError("host_root must exist as a directory")
        return str(resolved)

    raise HostRootError(
        "host_root must be a Windows drive-letter absolute directory "
        "(e.g. D:\\src\\recertia)"
    )


_DENIED_HOST_PARTS = frozenset({".recertia"})


def assert_host_root_allowed(host_root: str) -> str:
    """Allowlist from ``RECERTIA_WORKSPACE_ROOTS``, plus a ``.recertia`` denylist.

    An empty allowlist in ``single_node`` keeps today's registration behavior.
    ``multi_node`` refuses registration until the allowlist is set. A path
    whose components include ``.recertia`` is refused for every caller.
    """

    from recertia.settings import load_settings

    settings = load_settings()
    try:
        resolved = Path(host_root).resolve()
    except OSError as exc:
        raise HostRootError(f"host_root could not be resolved: {exc}") from exc
    if any(part in _DENIED_HOST_PARTS for part in resolved.parts):
        raise HostRootError("host_root is denied: path contains .recertia")
    roots = tuple(Path(item) for item in settings.workspace_roots)
    if not roots:
        if settings.deployment_mode == "multi_node":
            raise HostRootError("RECERTIA_WORKSPACE_ROOTS is required in multi_node")
        return str(resolved)
    for root in roots:
        try:
            base = root.resolve()
        except OSError as exc:
            raise HostRootError(f"workspace root could not be resolved: {exc}") from exc
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        return str(resolved)
    raise HostRootError("host_root is outside RECERTIA_WORKSPACE_ROOTS")


def tenant_skills_root(*, control_root: Path, shared: Path, tenant_id: str) -> Path:
    """Per-tenant skill library when the flag or ``multi_node`` asks for one.

    ``single_node`` keeps the shared ``skills/`` directory until
    ``RECERTIA_TENANT_SKILLS`` is on, so existing checkouts keep working.
    """

    from recertia.ports.identity import tenant_id as parse_tenant
    from recertia.settings import load_settings

    ident = str(parse_tenant(tenant_id))
    settings = load_settings()
    if settings.deployment_mode != "multi_node" and not settings.tenant_skills:
        return Path(shared)
    path = Path(control_root) / "tenants" / ident / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_under_host_root(host_root: str, subpath: str | None) -> Path:
    """Resolve ``subpath`` under a stored host root; refuse escapes."""

    try:
        root = Path(host_root).resolve()
    except OSError as exc:
        raise HostRootError(f"host_root could not be resolved: {exc}") from exc
    parts = split_rel_subpath(subpath)
    if not parts:
        return root
    try:
        return contained_path(root, *parts)
    except PathEscapeError as exc:
        raise HostRootError("workdir escapes registered workspace root") from exc
