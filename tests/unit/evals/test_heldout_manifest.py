"""REC-009: held-out hash manifest loader and fail-closed root hashing.

Every tree here is synthetic and built under ``tmp_path``. No held-out data is used.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import typing
from pathlib import Path
from typing import Any

import pytest

from recertia.evals import heldout_manifest as hm
from recertia.evals.heldout_manifest import (
    HeldoutManifest,
    HeldoutManifestError,
    compute_root_sha256,
    load_heldout_manifest,
    verify_heldout_tree,
)

REPO = Path(__file__).resolve().parents[3]
SCHEMA = REPO / "evals" / "heldout" / "heldout-manifest.schema.json"
_HEX_A = "a" * 64
_HEX_B = "b" * 64


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "split_id": "heldout-synthetic-chore-v1",
        "count": 1,
        "hash_rule": "sha256-sorted-relpath-v1",
        "root_sha256": _HEX_A,
        "ciphertext_sha256": _HEX_B,
        "created_at": "2026-10-21T12:00:00-07:00",
    }
    base.update(overrides)
    return base


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _tree(root: Path) -> Path:
    """Synthetic split: neutral dir names, a nested dir and a non-ASCII NFC name."""

    (root / "repo-chore" / "h001" / "workspace" / "src").mkdir(parents=True)
    (root / "repo-chore" / "h002").mkdir(parents=True)
    (root / "repo-chore" / "h001" / "task.json").write_text('{"x": 1}\n', encoding="utf-8")
    (root / "repo-chore" / "h001" / "workspace" / "src" / "café.txt").write_text(
        "synthetic\n", encoding="utf-8"
    )
    (root / "repo-chore" / "h002" / "task.json").write_text('{"x": 2}\n', encoding="utf-8")
    return root


def _manifest_for(tmp_path: Path, root: Path, **overrides: Any) -> Path:
    digest, count = compute_root_sha256(root)
    return _write(
        tmp_path / "m.manifest.json",
        _manifest(root_sha256=digest, count=count, **overrides),
    )


# ----- strict loader -----


def test_loader_accepts_a_valid_manifest(tmp_path: Path) -> None:
    loaded = load_heldout_manifest(_write(tmp_path / "m.json", _manifest()))
    assert loaded == HeldoutManifest(**_manifest())


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("schema_version bool", _manifest(schema_version=True)),
        ("schema_version str", _manifest(schema_version="1")),
        ("schema_version 2", _manifest(schema_version=2)),
        ("schema_version float", _manifest(schema_version=1.0)),
        ("hash_rule other", _manifest(hash_rule="sha256-sorted-relpath-v2")),
        ("root upper hex", _manifest(root_sha256="A" * 64)),
        ("root short", _manifest(root_sha256="a" * 63)),
        ("ciphertext non-hex", _manifest(ciphertext_sha256="g" * 64)),
        ("count zero", _manifest(count=0)),
        ("count bool", _manifest(count=True)),
        ("count str", _manifest(count="3")),
        ("split_id no prefix", _manifest(split_id="repo-chore-v1")),
        ("split_id no version", _manifest(split_id="heldout-repo-chore")),
        ("split_id upper", _manifest(split_id="heldout-Repo-v1")),
        ("created_at naive", _manifest(created_at="2026-10-21T12:00:00")),
        ("created_at date only", _manifest(created_at="2026-10-21")),
        ("created_at bad month", _manifest(created_at="2026-13-21T12:00:00-07:00")),
        ("old key created_at_pt", {**{k: v for k, v in _manifest().items() if k != "created_at"},
                                   "created_at_pt": "2026-10-21T12:00:00-07:00"}),
        ("unknown key", _manifest(files=[])),
        ("missing key", {k: v for k, v in _manifest().items() if k != "count"}),
        ("top-level list", [_manifest()]),
    ],
)
def test_loader_refuses_malformed_manifests(tmp_path: Path, label: str, payload: Any) -> None:
    path = _write(tmp_path / "m.json", payload)
    with pytest.raises(HeldoutManifestError):
        load_heldout_manifest(path)


def test_loader_refuses_duplicate_keys_trailing_data_and_nan(tmp_path: Path) -> None:
    body = json.dumps(_manifest())
    dup = tmp_path / "dup.json"
    dup.write_text(body[:-1] + ', "count": 1}', encoding="utf-8")
    trailing = tmp_path / "trailing.json"
    trailing.write_text(body + "\n{}\n", encoding="utf-8")
    nan = tmp_path / "nan.json"
    nan.write_text(body.replace('"count": 1', '"count": NaN'), encoding="utf-8")
    for path in (dup, trailing, nan):
        with pytest.raises(HeldoutManifestError):
            load_heldout_manifest(path)


def test_loader_accepts_utc_z_offset(tmp_path: Path) -> None:
    path = _write(tmp_path / "m.json", _manifest(created_at="2026-10-21T19:00:00Z"))
    assert load_heldout_manifest(path).created_at == "2026-10-21T19:00:00Z"


def test_heldout_manifest_is_a_concrete_frozen_dataclass() -> None:
    assert isinstance(HeldoutManifest, type)
    assert dataclasses.is_dataclass(HeldoutManifest)
    assert HeldoutManifest.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert not getattr(HeldoutManifest, "_is_protocol", False)
    assert typing.Protocol not in HeldoutManifest.__mro__
    assert hm.__all__ == [
        "HeldoutManifest",
        "HeldoutManifestError",
        "verify_heldout_tree",
        "load_heldout_manifest",
        "compute_root_sha256",
    ]

    class _Duck:
        def __init__(self) -> None:
            for key, value in _manifest().items():
                setattr(self, key, value)

    assert not isinstance(_Duck(), HeldoutManifest)
    instance = HeldoutManifest(**_manifest())
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.count = 2  # type: ignore[misc]


def test_committed_schema_matches_loader_and_holds_no_data() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(hm.MANIFEST_KEYS)
    assert set(schema["properties"]) == set(hm.MANIFEST_KEYS)
    assert [f.name for f in dataclasses.fields(HeldoutManifest)] == list(hm.MANIFEST_KEYS)
    assert schema["properties"]["hash_rule"] == {"const": hm.HASH_RULE}
    assert schema["properties"]["schema_version"] == {"const": hm.SCHEMA_VERSION}
    # Schema only: no example, default, or enum values that could carry split data.
    text = SCHEMA.read_text(encoding="utf-8")
    for word in ("examples", "default", "enum"):
        assert f'"{word}"' not in text


# ----- fail-closed root hashing -----


def test_root_hash_matches_find_sort_sha256sum(tmp_path: Path) -> None:
    if shutil.which("find") is None or shutil.which("sha256sum") is None:
        pytest.skip("GNU find and sha256sum are required for the equivalence check")
    root = _tree(tmp_path / "split")
    digest, count = compute_root_sha256(root)
    shell = subprocess.run(
        [
            "bash",
            "-c",
            "find . -type f -printf '%P\\0' | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert digest == shell.stdout.decode().split()[0]
    assert count == 3


def test_root_hash_is_independent_of_modes_and_mtimes(tmp_path: Path) -> None:
    root = _tree(tmp_path / "split")
    before = compute_root_sha256(root)
    target = root / "repo-chore" / "h002" / "task.json"
    os.chmod(target, 0o600)
    os.utime(target, (1, 1))
    assert compute_root_sha256(root) == before


def test_root_hash_changes_when_a_byte_or_a_name_changes(tmp_path: Path) -> None:
    root = _tree(tmp_path / "split")
    before = compute_root_sha256(root)
    (root / "repo-chore" / "h002" / "task.json").write_text('{"x": 3}\n', encoding="utf-8")
    after_edit = compute_root_sha256(root)
    assert after_edit[0] != before[0]
    os.rename(root / "repo-chore" / "h002", root / "repo-chore" / "h003")
    assert compute_root_sha256(root)[0] != after_edit[0]


def _refuses(root: Path) -> str:
    with pytest.raises(HeldoutManifestError) as exc:
        compute_root_sha256(root)
    return str(exc.value)


def test_root_hash_refuses_symlinks_empty_dirs_and_special_files(tmp_path: Path) -> None:
    sym = _tree(tmp_path / "sym")
    (sym / "repo-chore" / "h001" / "link").symlink_to(sym / "repo-chore" / "h002" / "task.json")
    assert "symlink" in _refuses(sym)

    out_of_tree = _tree(tmp_path / "oot")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (out_of_tree / "repo-chore" / "escape").symlink_to(outside)
    assert "symlink" in _refuses(out_of_tree)

    empty = _tree(tmp_path / "empty")
    (empty / "repo-chore" / "h009").mkdir()
    assert "empty directory" in _refuses(empty)

    fifo = _tree(tmp_path / "fifo")
    os.mkfifo(fifo / "repo-chore" / "pipe")
    assert "special file" in _refuses(fifo)

    real = _tree(tmp_path / "real")
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real, target_is_directory=True)
    assert "root is a symlink" in _refuses(link_root)


@pytest.mark.parametrize(
    "name",
    [
        b"new\nline.txt",
        b"carriage\rreturn.txt",
        b"back\\slash.txt",
        b"latin1-\xe9.txt",  # not UTF-8
        "cafe\u0301.txt".encode(),  # NFD, not NFC: refused, never normalized
    ],
)
def test_root_hash_refuses_unsafe_names(tmp_path: Path, name: bytes) -> None:
    root = _tree(tmp_path / "split")
    target = os.path.join(os.fsencode(root / "repo-chore" / "h002"), name)
    with open(target, "wb") as handle:
        handle.write(b"synthetic\n")
    message = _refuses(root)
    assert os.fsdecode(name) not in message


# ----- single entry point -----


def test_verify_heldout_tree_returns_the_manifest(tmp_path: Path) -> None:
    root = _tree(tmp_path / "split")
    manifest_path = _manifest_for(tmp_path, root)
    result = verify_heldout_tree(manifest_path, root)
    assert isinstance(result, HeldoutManifest)
    assert result.count == 3


def test_verify_heldout_tree_refuses_hash_mismatch_with_hashes_only(tmp_path: Path) -> None:
    root = _tree(tmp_path / "split")
    manifest_path = _manifest_for(tmp_path, root)
    expected = load_heldout_manifest(manifest_path).root_sha256
    (root / "repo-chore" / "h002" / "task.json").write_text('{"x": 9}\n', encoding="utf-8")
    actual, _ = compute_root_sha256(root)
    with pytest.raises(HeldoutManifestError) as exc:
        verify_heldout_tree(manifest_path, root)
    message = str(exc.value)
    assert message.startswith("held-out hash mismatch")
    assert expected in message and actual in message
    for leaked in ("h001", "h002", "repo-chore", "task.json", "café", str(root)):
        assert leaked not in message


def test_verify_heldout_tree_refuses_count_mismatch(tmp_path: Path) -> None:
    root = _tree(tmp_path / "split")
    digest, count = compute_root_sha256(root)
    manifest_path = _write(tmp_path / "m.json", _manifest(root_sha256=digest, count=count + 1))
    with pytest.raises(HeldoutManifestError, match="^held-out count mismatch"):
        verify_heldout_tree(manifest_path, root)


def test_verify_heldout_tree_refuses_unsafe_roots(tmp_path: Path) -> None:
    root = _tree(tmp_path / "split")
    manifest_path = _manifest_for(tmp_path, root)

    link_root = tmp_path / "link-root"
    link_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(HeldoutManifestError, match="symlink"):
        verify_heldout_tree(manifest_path, link_root)

    inside = root / "repo-chore" / "h002" / "m.manifest.json"
    shutil.copy(manifest_path, inside)
    with pytest.raises(HeldoutManifestError, match="manifest lies inside"):
        verify_heldout_tree(inside, root)
    inside.unlink()

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    in_repo = _tree(checkout / "evals" / "decrypted")
    with pytest.raises(HeldoutManifestError, match="inside a git checkout"):
        verify_heldout_tree(_manifest_for(tmp_path, in_repo), in_repo)


def test_verify_heldout_tree_refuses_the_real_checkout(tmp_path: Path) -> None:
    """Amendment 1: never decrypt held-out data inside the checkout."""

    if not (REPO / ".git").exists():
        pytest.skip("not running from a git checkout")
    manifest_path = _write(tmp_path / "m.json", _manifest())
    with pytest.raises(HeldoutManifestError, match="inside a git checkout"):
        verify_heldout_tree(manifest_path, REPO / "evals" / "golden")


def test_verify_heldout_tree_refuses_a_bad_manifest_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "split")
    bad = _write(tmp_path / "m.json", _manifest(schema_version="1"))
    calls: list[Path] = []

    def _spy(root: Path) -> tuple[str, int]:
        calls.append(root)
        return "", 0

    monkeypatch.setattr(hm, "compute_root_sha256", _spy)
    with pytest.raises(HeldoutManifestError):
        verify_heldout_tree(bad, root)
    assert calls == []


def test_sha256_line_format_is_two_spaces_and_newline(tmp_path: Path) -> None:
    root = tmp_path / "one"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x")
    file_hash = hashlib.sha256(b"x").hexdigest()
    expected = hashlib.sha256(f"{file_hash}  a.txt\n".encode()).hexdigest()
    assert compute_root_sha256(root) == (expected, 1)
