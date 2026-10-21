"""Held-out split hash manifest: strict loader and fail-closed tree hashing (REC-009).

The private held-out split never enters the repository. Only a public manifest is
committed, under ``evals/heldout/`` (never ``evals/golden/``), and it holds exactly
one root hash, the ciphertext hash and a file count. There are no per-file or
per-task rows. The schema is ``evals/heldout/heldout-manifest.schema.json``.

Hash rule ``sha256-sorted-relpath-v1``:

* for each regular file, take its relpath from the realpath'd split root, with
  ``/`` separators and no leading ``./``;
* form the line ``"<sha256 hex>  <relpath>\\n"``;
* sort the lines bytewise (UTF-8) by relpath, concatenate them, and hash the result.

This equals ``cd root && find . -type f -printf '%P\\0' | LC_ALL=C sort -z |
xargs -0 sha256sum | sha256sum``. Symlinks (including a symlinked root), empty
directories, special files, non-UTF-8 names, names that are not already NFC and
names holding a newline, CR or backslash all refuse; nothing is normalized.

:func:`verify_heldout_tree` is the single entry point. It never discovers a split:
the caller passes the manifest and the decrypted root explicitly. Errors carry only
hashes and counts, never file or fixture names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "HeldoutManifest",
    "HeldoutManifestError",
    "verify_heldout_tree",
    "load_heldout_manifest",
    "compute_root_sha256",
]

SCHEMA_VERSION = 1
HASH_RULE = "sha256-sorted-relpath-v1"
MANIFEST_KEYS = (
    "schema_version",
    "split_id",
    "count",
    "hash_rule",
    "root_sha256",
    "ciphertext_sha256",
    "created_at",
)
_MAX_MANIFEST_BYTES = 64 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SPLIT_ID = re.compile(r"heldout-[a-z0-9-]+-v[0-9]+")
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_BAD_NAME_CHARS = ("\n", "\r", "\\")
_CHUNK = 1024 * 1024


class HeldoutManifestError(Exception):
    """Every refusal and mismatch. Messages carry hashes and counts, never names."""


@dataclass(frozen=True)
class HeldoutManifest:
    schema_version: int
    split_id: str
    count: int
    hash_rule: str
    root_sha256: str
    ciphertext_sha256: str
    created_at: str


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise HeldoutManifestError("manifest refused: duplicate key")
        out[key] = value
    return out


def _no_constants(token: str) -> Any:
    raise HeldoutManifestError("manifest refused: non-finite number")


def load_heldout_manifest(path: Path) -> HeldoutManifest:
    """Parse and validate a public manifest. Anything unexpected refuses."""

    path = Path(path)
    if path.is_symlink():
        raise HeldoutManifestError("manifest refused: symlink")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HeldoutManifestError("manifest refused: unreadable") from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise HeldoutManifestError("manifest refused: too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeldoutManifestError("manifest refused: not UTF-8") from exc
    try:
        data = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants
        )
    except json.JSONDecodeError as exc:
        # Also covers trailing data after the top-level object ("Extra data").
        raise HeldoutManifestError("manifest refused: invalid JSON or trailing data") from exc
    if not isinstance(data, dict):
        raise HeldoutManifestError("manifest refused: top level must be an object")
    keys = set(data)
    if keys != set(MANIFEST_KEYS):
        unknown = len(keys - set(MANIFEST_KEYS))
        missing = len(set(MANIFEST_KEYS) - keys)
        raise HeldoutManifestError(
            f"manifest refused: {unknown} unknown and {missing} missing key(s)"
        )

    version = data["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise HeldoutManifestError("manifest refused: schema_version must be the integer 1")
    split_id = data["split_id"]
    if type(split_id) is not str or not _SPLIT_ID.fullmatch(split_id):
        raise HeldoutManifestError("manifest refused: split_id format")
    count = data["count"]
    if type(count) is not int or count < 1:
        raise HeldoutManifestError("manifest refused: count must be an integer >= 1")
    hash_rule = data["hash_rule"]
    if type(hash_rule) is not str or hash_rule != HASH_RULE:
        raise HeldoutManifestError(f"manifest refused: hash_rule must be {HASH_RULE!r}")
    for key in ("root_sha256", "ciphertext_sha256"):
        value = data[key]
        if type(value) is not str or not _HEX64.fullmatch(value):
            raise HeldoutManifestError(f"manifest refused: {key} must be 64 lowercase hex")
    created_at = data["created_at"]
    if type(created_at) is not str or not _RFC3339.fullmatch(created_at):
        raise HeldoutManifestError(
            "manifest refused: created_at must be RFC 3339 with an explicit offset"
        )
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HeldoutManifestError("manifest refused: created_at is not a valid time") from exc
    if parsed.tzinfo is None:
        raise HeldoutManifestError("manifest refused: created_at needs an offset")
    return HeldoutManifest(
        schema_version=version,
        split_id=split_id,
        count=count,
        hash_rule=hash_rule,
        root_sha256=data["root_sha256"],
        ciphertext_sha256=data["ciphertext_sha256"],
        created_at=created_at,
    )


def _checked_name(name: str) -> bytes:
    raw = os.fsencode(name)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeldoutManifestError("held-out tree refused: non-UTF-8 name") from exc
    if any(ch in decoded for ch in _BAD_NAME_CHARS):
        raise HeldoutManifestError("held-out tree refused: newline, CR or backslash in a name")
    if not unicodedata.is_normalized("NFC", decoded):
        raise HeldoutManifestError("held-out tree refused: name is not NFC")
    return raw


def _file_sha256(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HeldoutManifestError("held-out tree refused: unreadable file") from exc
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise HeldoutManifestError("held-out tree refused: special file")
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(directory: str, prefix: bytes, out: list[tuple[bytes, str]]) -> None:
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise HeldoutManifestError("held-out tree refused: unreadable directory") from exc
    if not entries:
        raise HeldoutManifestError("held-out tree refused: empty directory")
    for entry in entries:
        name = _checked_name(entry.name)
        rel = prefix + name
        mode = os.lstat(entry.path).st_mode
        if stat.S_ISLNK(mode):
            raise HeldoutManifestError("held-out tree refused: symlink")
        if stat.S_ISDIR(mode):
            _walk(entry.path, rel + b"/", out)
        elif stat.S_ISREG(mode):
            out.append((rel, _file_sha256(entry.path)))
        else:
            raise HeldoutManifestError("held-out tree refused: special file")


def compute_root_sha256(root: Path) -> tuple[str, int]:
    """Return ``(root_sha256, file_count)`` under ``sha256-sorted-relpath-v1``."""

    root = Path(root)
    if root.is_symlink():
        raise HeldoutManifestError("held-out tree refused: root is a symlink")
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        raise HeldoutManifestError("held-out tree refused: root is not a directory")
    rows: list[tuple[bytes, str]] = []
    _walk(real, b"", rows)
    rows.sort(key=lambda row: row[0])
    digest = hashlib.sha256()
    for rel, file_hash in rows:
        digest.update(file_hash.encode("ascii") + b"  " + rel + b"\n")
    return digest.hexdigest(), len(rows)


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _git_checkout_above(path: str) -> bool:
    current = Path(path)
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return True
    return False


def verify_heldout_tree(manifest_path: Path, root: Path) -> HeldoutManifest:
    """Load the manifest, refuse unsafe roots, hash the tree and compare. Fails closed."""

    manifest = load_heldout_manifest(manifest_path)
    root = Path(root)
    if root.is_symlink():
        raise HeldoutManifestError("held-out tree refused: root is a symlink")
    real_root = os.path.realpath(root)
    if _git_checkout_above(real_root):
        raise HeldoutManifestError(
            "held-out tree refused: root is inside a git checkout (decrypt outside the repo)"
        )
    if _inside(os.path.realpath(manifest_path), real_root):
        raise HeldoutManifestError("held-out tree refused: manifest lies inside the split root")
    actual_root, actual_count = compute_root_sha256(Path(real_root))
    if actual_root != manifest.root_sha256 or actual_count != manifest.count:
        kind = "count" if actual_count != manifest.count else "hash"
        raise HeldoutManifestError(
            f"held-out {kind} mismatch: manifest root_sha256={manifest.root_sha256} "
            f"count={manifest.count}; tree root_sha256={actual_root} count={actual_count}"
        )
    return manifest
