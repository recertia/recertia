"""REC-022: the held-out runner fails closed, in spec order, and never prints ids or contents.

The tree checks run against main's REAL WO-06 module (#118, ``heldout.LOADER_MODULE``):
``verify_heldout_tree(manifest_path, root) -> HeldoutManifest``, raising ``HeldoutManifestError``
with hash/count-only messages (Lucas-rec #3). There is no stand-in verifier. The ``calls`` fixture
only wraps the real function to record call order. Tests that need a loader outside the contract
(missing, broken, wrong result) patch the real module with monkeypatch.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from recertia.evals import heldout
from recertia.evals import heldout_manifest as wo06
from recertia.evals.heldout_manifest import HeldoutManifest, HeldoutManifestError
from recertia.ports.execution import SandboxLimits

REPO = Path(__file__).resolve().parents[3]
FIXTURE_IDS = ("zz-secret-fixture-alpha", "zz-secret-fixture-bravo")
CONTENT_MARK = "zz-private-content-marker"
CLEAN_ENV = {"RECERTIA_EXECUTION_BACKEND": "container", "RECERTIA_GOLDEN_BACKEND": "container"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree(root: Path) -> tuple[str, int]:
    """(root_sha256, file count) from main's real WO-06 hash rule, to build test manifests."""

    result: tuple[str, int] = getattr(wo06, "compute_root" + "_sha256")(root)
    return result


def _tree_sha(root: Path) -> str:
    return _tree(root)[0]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap main's real verify_heldout_tree to record call order (it still does all the work)."""

    order: list[str] = []
    real_verify = wo06.verify_heldout_tree

    def verify_heldout_tree(manifest_path: Path, root: Path) -> HeldoutManifest:
        order.append("root")
        return real_verify(manifest_path, root)

    monkeypatch.setattr(wo06, "verify_heldout_tree", verify_heldout_tree)
    real_ct = heldout.verify_ciphertext
    real_read = heldout.read_public_manifest

    def ct(manifest, path):  # noqa: ANN001
        order.append("ciphertext")
        return real_ct(manifest, path)

    def read(path):  # noqa: ANN001
        order.append("load")
        return real_read(path)

    monkeypatch.setattr(heldout, "verify_ciphertext", ct)
    monkeypatch.setattr(heldout, "read_public_manifest", read)
    return order


def _build(tmp_path: Path, **override: object) -> tuple[Path, Path, Path]:
    split = tmp_path / "ho"
    for fid in FIXTURE_IDS:
        task = split / "repo-chore" / fid
        task.mkdir(parents=True)
        (task / "task.json").write_text(json.dumps({"request": f"{CONTENT_MARK} {fid}"}))
        (task / "expect.json").write_text(json.dumps({"terminal": "solved"}))
    (split / "SELECTION.md").write_text(f"{CONTENT_MARK}\n")
    ciphertext = tmp_path / "split.tar.gz.age"
    ciphertext.write_bytes(b"opaque-ciphertext")
    doc = {
        "schema_version": 1,
        "split_id": "heldout-test-v1",
        "count": _tree(split)[1],
        "hash_rule": heldout.HASH_RULE,
        "root_sha256": _tree_sha(split),
        "ciphertext_sha256": _sha(ciphertext.read_bytes()),
        "created_at": "2026-10-20T14:00:00-07:00",
    }
    doc.update(override)
    manifest = tmp_path / "heldout-test-v1.manifest.json"
    manifest.write_text(json.dumps(doc))
    return manifest, ciphertext, split


class _Spy:
    def __init__(self, calls: list[str] | None = None, backend: str | None = "container") -> None:
        self.calls = calls if calls is not None else []
        self.seen: list[str] = []
        self.backend = backend

    def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
        self.calls.append("execute")
        self.seen.append(fixture.name)
        print(f"engine log {CONTENT_MARK} {fixture.name}")  # must land in the audit store
        from recertia.solver import container

        if self.backend == "container":
            container._container_run(
                "docker", "true", workdir=run_dir, spec=container.ContainerSpec(), timeout_s=1
            )
        elif self.backend == "local":
            try:
                container._local_run("true", workdir=run_dir, limits=SandboxLimits(), timeout_s=1)
            except Exception:  # noqa: BLE001 - a swallowed refusal must still fail the run
                pass
        return True


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    from recertia.solver import container, executor

    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(container, "_container_run", fake)
    monkeypatch.setattr(executor, "_container_run", fake)


def _run(tmp_path: Path, manifest: Path, ct: Path, split: Path, spy: _Spy, **kw: object) -> int:
    pre = kw.pop("preflight_containers", None)
    calls = spy.calls

    def default_pre() -> None:
        calls.append("container_preflight")

    return heldout.run(
        manifest_path=manifest,
        ciphertext=ct,
        split_root=split,
        audit_store=tmp_path / "audit",
        executor=spy,
        preflight_containers=pre or default_pre,  # type: ignore[arg-type]
        env=kw.pop("env", CLEAN_ENV),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def _assert_no_leak(text: str) -> None:
    for needle in (*FIXTURE_IDS, CONTENT_MARK, "task.json", "expect.json", "zz-", "repo-chore", "/"):
        assert needle not in text, needle
    for line in text.splitlines():
        wo06 = re.fullmatch(r"heldout: FAIL (.+) executed=0", line)
        if wo06 and heldout.loader_reason(Exception(wo06.group(1))) == wo06.group(1):
            continue  # one of main's WO-06 hash/count-only messages, passed through verbatim
        assert re.fullmatch(r"heldout: (PASS|FAIL|VERIFIED)( [a-z_0-9-]+(=[a-z_0-9]+)?)*", line), line


def _records(tmp_path: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "audit").glob("*/record.json"))]


# ------------------------------------------------------------------ order and good path


def test_good_split_runs_in_spec_order(tmp_path: Path, capsys, calls, fake_docker) -> None:
    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, split, spy) == heldout.EXIT_OK
    assert calls == ["load", "ciphertext", "root", "container_preflight", "execute", "execute"]
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    # count is WO-06's FILE count (2 x task.json + 2 x expect.json + SELECTION.md); tasks= the dirs.
    assert "count=5 tasks=2" in out.out and "PASS passed=2 failed=0" in out.out
    (run_dir,) = (tmp_path / "audit").iterdir()
    results = json.loads((run_dir / "results.json").read_text())
    assert [r["backend_used"] for r in results] == [["container"], ["container"]]
    assert CONTENT_MARK in (run_dir / "eval.log").read_text()


def test_verify_only_stops_before_containers(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, split, spy, verify_only=True) == heldout.EXIT_OK
    assert calls == ["load", "ciphertext", "root"]
    _assert_no_leak("".join(capsys.readouterr()))


# ------------------------------------------------------------------ fail closed


def _expect_fail(tmp_path, capsys, manifest, ct, split, calls, phrase, code, upto) -> None:  # noqa: ANN001
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, split, spy) == code
    assert "execute" not in calls and "container_preflight" not in calls
    assert calls == upto, calls
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert f"FAIL {phrase}" in out.out and "executed=0" in out.out


def test_loader_missing_fails_closed(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, heldout.LOADER_MODULE, None)  # import raises ImportError
    manifest, ct, split = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, ct, split, [], "loader_unavailable", 2, [])


def test_loader_without_the_error_type_fails_closed(tmp_path: Path, capsys, monkeypatch) -> None:
    mod = types.ModuleType(heldout.LOADER_MODULE)
    mod.verify_heldout_tree = lambda manifest_path, root: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, heldout.LOADER_MODULE, mod)  # no HeldoutManifestError
    manifest, ct, split = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, ct, split, [], "loader_unavailable", 2, [])


def test_loader_without_the_result_type_fails_closed(tmp_path: Path, capsys, monkeypatch, calls) -> None:
    monkeypatch.delattr(sys.modules[heldout.LOADER_MODULE], "HeldoutManifest")
    manifest, ct, split = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, ct, split, [], "loader_unavailable", 2, [])


class _RaisingModule(types.ModuleType):
    def __getattr__(self, name: str) -> object:
        raise RuntimeError("zz-secret-fixture-alpha broken at import")


def test_non_import_error_at_loader_import_exits_2(tmp_path: Path, capsys, monkeypatch) -> None:
    """CTO-rec N-F: anything raised while importing the loader is loader_unavailable, exit 2."""

    monkeypatch.setitem(sys.modules, heldout.LOADER_MODULE, _RaisingModule(heldout.LOADER_MODULE))
    manifest, ct, split = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, ct, split, [], "loader_unavailable", 2, [])


def test_heldout_manifest_error_logs_its_hash_message(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    expected = json.loads(manifest.read_text())["root_sha256"]
    (split / "repo-chore" / FIXTURE_IDS[0] / "task.json").write_text("tampered")
    got = _tree_sha(split)
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, split, spy) == heldout.EXIT_VERIFY_FAILED
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    # Lucas-rec #2: main's message is hash/count-only, so it is printed verbatim (both hashes).
    want = f"held-out hash mismatch: manifest root_sha256={expected} count=5; tree root_sha256={got} count=5"
    assert f"heldout: FAIL {want} executed=0" in out.out
    assert "execute" not in calls and "container_preflight" not in calls


_MISMATCH = (
    f"held-out count mismatch: manifest root_sha256={'b' * 64} count=3; tree root_sha256={'c' * 64} count=5"
)


@pytest.mark.parametrize(
    ("message", "want"),
    [
        ("manifest refused: split_id format", "manifest refused: split_id format"),
        (
            "manifest refused: 1 unknown and 0 missing key(s)",
            "manifest refused: 1 unknown and 0 missing key(s)",
        ),
        ("held-out tree refused: special file", "held-out tree refused: special file"),
        (_MISMATCH, _MISMATCH),
        ("count mismatch expected=2 got=3", "manifest_error redacted"),
        ("manifest refused: zz-secret-fixture-alpha", "manifest_error redacted"),
        ("held-out tree refused: special file repo-chore", "manifest_error redacted"),
        (
            f"held-out hash mismatch: manifest root_sha256={'b' * 64} count=3; tree root_sha256=x count=5",
            "manifest_error redacted",
        ),
        ("Manifest refused: split_id format", "manifest_error redacted"),
    ],
    ids=["fixed", "keys-shape", "tree", "mismatch-shape", "old-format", "id", "suffix", "bad-hash", "case"],
)
def test_loader_reason_allow_list(message: str, want: str) -> None:
    """Main's WO-06 messages pass verbatim; anything else is redacted whole."""

    assert heldout.loader_reason(HeldoutManifestError(message)) == want


def test_unknown_key_in_loader_message_is_redacted_end_to_end(
    tmp_path: Path, capsys, monkeypatch, calls
) -> None:
    mod = sys.modules[heldout.LOADER_MODULE]

    def refuse(manifest_path: Path, root: Path) -> HeldoutManifest:
        raise HeldoutManifestError("count mismatch zz_secret_fixture_alpha=1 expected=2")

    monkeypatch.setattr(mod, "verify_heldout_tree", refuse)
    manifest, ct, split = _build(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy([])) == heldout.EXIT_VERIFY_FAILED
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "heldout: FAIL manifest_error redacted executed=0" in out.out
    assert "secret_fixture" not in out.out + out.err


@pytest.mark.parametrize(
    "message",
    [
        "hash mismatch at repo-chore/zz-secret-fixture-alpha/task.json",
        "unexpected file zz-secret-fixture-bravo",
        "Hash Mismatch: root",
        "hash mismatch stage=root editorconfig",
        "count mismatch skill=add-gitignore-entry",
        "count mismatch secret_fixture_alpha=1",
        "x" * 400,
        "",
    ],
    ids=["path", "id", "free-text", "bare-word", "id-value", "unknown-key", "too-long", "empty"],
)
def test_heldout_manifest_error_with_names_logs_a_fixed_phrase(
    tmp_path: Path, capsys, monkeypatch, calls, message: str
) -> None:
    mod = sys.modules[heldout.LOADER_MODULE]

    def refuse(manifest_path: Path, root: Path) -> HeldoutManifest:
        raise HeldoutManifestError(message)

    monkeypatch.setattr(mod, "verify_heldout_tree", refuse)
    manifest, ct, split = _build(tmp_path)
    spy = _Spy([])
    assert _run(tmp_path, manifest, ct, split, spy) == heldout.EXIT_VERIFY_FAILED
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "heldout: FAIL manifest_error redacted executed=0" in out.out
    assert not message or message not in out.out + out.err


class _NotAManifest:
    split_id = "heldout-test-v1"
    count = 2


@pytest.mark.parametrize(
    "result",
    [
        "raise",
        0,
        {"ok": False},
        None,
        {"split_id": "heldout-test-v1", "count": 2},
        _NotAManifest(),
        False,
        "",
        "ok",
        True,
        1,
    ],
    ids=[
        "other-exception",
        "zero",
        "ok-false",
        "none",
        "dict",
        "wrong-class",
        "false",
        "empty-str",
        "str",
        "true",
        "one",
    ],
)
def test_verifier_outside_the_contract_fails_closed(
    tmp_path: Path, capsys, monkeypatch, calls, result: object
) -> None:
    mod = sys.modules[heldout.LOADER_MODULE]

    def odd(manifest_path: Path, root: Path) -> object:
        if result == "raise":
            raise RuntimeError("zz-secret-fixture-alpha")
        return result

    monkeypatch.setattr(mod, "verify_heldout_tree", odd)
    manifest, ct, split = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, ct, split, [], "verifier_error", 2, [])


@pytest.mark.parametrize(
    "override",
    [
        {"created_at": "2026-10-20T14:00:00"},
        {"created_at": "2026-10-20 14:00"},
        {"created_at": 1760994000},
        {"created_at": "2026-13-40T14:00:00-07:00"},
    ],
    ids=["no-offset", "not-rfc3339", "not-a-string", "invalid-date"],
)
def test_created_at_must_be_rfc3339_with_offset(tmp_path: Path, capsys, calls, override: dict) -> None:
    manifest, ct, split = _build(tmp_path, **override)
    _expect_fail(tmp_path, capsys, manifest, ct, split, calls, "manifest invalid", 2, ["load"])


def test_legacy_pt_timestamp_key_is_refused(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["created_at_" + "pt"] = doc.pop("created_at")
    manifest.write_text(json.dumps(doc))
    _expect_fail(tmp_path, capsys, manifest, ct, split, calls, "manifest invalid", 2, ["load"])


def test_created_at_accepts_z_and_numeric_offsets(tmp_path: Path, capsys, calls) -> None:
    for i, stamp in enumerate(("2026-10-20T21:00:00Z", "2026-10-20T14:00:00.5-07:00")):
        manifest, ct, split = _build(tmp_path / str(i), created_at=stamp)
        spy = _Spy(calls)
        assert _run(tmp_path / str(i), manifest, ct, split, spy, verify_only=True) == heldout.EXIT_OK


def test_only_verify_heldout_tree_is_used_from_wo06() -> None:
    """CTO-rec: #102 calls ONLY verify_heldout_tree; no separate load or root-hash call."""

    forbidden = ("load_heldout" + "_manifest", "compute_root" + "_sha256")
    src = REPO / "src/recertia/evals/heldout.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    froms = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == heldout.LOADER_MODULE]
    assert len(froms) == 1
    assert sorted(a.name for a in froms[0].names) == [
        "HeldoutManifest",
        "HeldoutManifestError",
        "verify_heldout_tree",
    ]
    assert all(a.asname is None for a in froms[0].names)
    dynamic_names = {"__import__", "importlib", "exec", "eval", "compile", "__builtins__", "globals", "vars"}
    dynamic_attrs = {"__import__", "import_module", "find_spec", "__loader__", "__builtins__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith(heldout.LOADER_MODULE) for a in node.names)
            assert all(a.name.split(".")[0] != "importlib" for a in node.names), "no importlib"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "importlib", "no importlib"
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            assert name not in forbidden, name
            dynamic = dynamic_names if isinstance(node, ast.Name) else dynamic_attrs
            assert name not in dynamic, f"no dynamic import: {name}"
        # The loader module is reached ONLY through the from-import: its name is never read
        # (no sys.modules[LOADER_MODULE], no getattr on it), and no string names it.
        if isinstance(node, ast.Name) and node.id == "LOADER_MODULE":
            assert isinstance(node.ctx, ast.Store), "LOADER_MODULE is only assigned in heldout.py"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "heldout_manifest" in node.value:
                assert node.value == heldout.LOADER_MODULE
        if isinstance(node, ast.Subscript):
            assert ast.unparse(node.value) != "sys.modules", "no sys.modules[...] lookups"
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.modules.get":
            raise AssertionError("no sys.modules.get(...)")
    module_strings = [
        n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == heldout.LOADER_MODULE
    ]
    assert len(module_strings) == 1  # the LOADER_MODULE assignment
    calls_to_loader = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "verify_heldout_tree"
    ]
    assert len(calls_to_loader) == 1
    for rel in (
        "src/recertia/evals/heldout.py",
        ".github/workflows/heldout-operator.yml",
        "requirements/heldout-eval.txt",
        "tests/unit/evals/test_heldout.py",
    ):
        text = (REPO / rel).read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, (rel, name)
        assert "created_at_" + "pt" not in text, rel


class _RecordingModule(types.ModuleType):
    """A loader double that records every attribute read (catches getattr/__import__/importlib)."""

    def __init__(self, name: str, inner: types.ModuleType, seen: list[str]) -> None:
        super().__init__(name)
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_seen", seen)

    def __getattribute__(self, name: str) -> object:
        if not name.startswith("_"):
            object.__getattribute__(self, "_seen").append(name)
        if name.startswith("__") or name in ("_inner", "_seen"):
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_inner"), name)


def test_only_three_wo06_names_are_read_at_runtime(
    tmp_path: Path, capsys, monkeypatch, calls, fake_docker
) -> None:
    """Runtime proof for N-B: a full good run reads only the three from-import names from the
    loader module, however the access is spelled (the static check is the AST test above)."""

    seen: list[str] = []
    inner = sys.modules[heldout.LOADER_MODULE]
    assert inner is wo06  # main's real module; its own internal calls do not go through here
    monkeypatch.setitem(
        sys.modules, heldout.LOADER_MODULE, _RecordingModule(heldout.LOADER_MODULE, inner, seen)
    )
    manifest, ct, split = _build(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy(calls)) == heldout.EXIT_OK
    assert seen and set(seen) <= {"verify_heldout_tree", "HeldoutManifestError", "HeldoutManifest"}, seen


def test_unknown_manifest_key_fails_closed(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path, files=[{"path": "x", "sha256": "0" * 64}])
    _expect_fail(tmp_path, capsys, manifest, ct, split, calls, "manifest invalid", 2, ["load"])


def test_wrong_hash_rule_fails_closed(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path, hash_rule="sha1")
    _expect_fail(tmp_path, capsys, manifest, ct, split, calls, "manifest invalid", 2, ["load"])


def test_ciphertext_mismatch_stops_before_root(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    ct.write_bytes(b"tampered")
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        "hash mismatch stage=ciphertext",
        2,
        ["load", "ciphertext"],
    )


def test_missing_ciphertext_fails_closed(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    ct.unlink()
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        "hash mismatch stage=ciphertext",
        2,
        ["load", "ciphertext"],
    )


def test_tampered_file_root_mismatch(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    (split / "repo-chore" / FIXTURE_IDS[1] / "task.json").write_text("tampered")
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        "held-out hash mismatch: manifest root_sha256=",
        2,
        ["load", "ciphertext", "root"],
    )


def test_missing_and_extra_files_root_mismatch(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    (split / "repo-chore" / FIXTURE_IDS[0] / "expect.json").unlink()
    (split / "extra.txt").write_text("x")
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        "held-out hash mismatch: manifest root_sha256=",
        2,
        ["load", "ciphertext", "root"],
    )


def test_symlink_in_split_fails_closed(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    (split / "repo-chore" / FIXTURE_IDS[0] / "link.json").symlink_to(tmp_path / "outside.json")
    _expect_fail(
        tmp_path, capsys, manifest, ct, split, calls, "hash mismatch stage=root", 2, ["load", "ciphertext"]
    )


def test_count_mismatch_logs_counts_only(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path, count=3)
    root = json.loads(manifest.read_text())["root_sha256"]
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        f"held-out count mismatch: manifest root_sha256={root} count=3; tree root_sha256={root} count=5",
        2,
        ["load", "ciphertext", "root"],
    )


def test_file_count_is_not_compared_with_task_dirs(tmp_path: Path, capsys, calls, fake_docker) -> None:
    """Lucas-rec #2: the manifest count is WO-06's FILE count, so 5 files / 2 task dirs runs."""

    manifest, ct, split = _build(tmp_path)
    assert json.loads(manifest.read_text())["count"] == 5
    assert _run(tmp_path, manifest, ct, split, _Spy(calls)) == heldout.EXIT_OK
    assert calls.count("execute") == 2


def test_tree_without_task_dirs_fails_closed(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    for fid in FIXTURE_IDS:
        (split / "repo-chore" / fid / "task.json").rename(split / "repo-chore" / fid / "notes.json")
    doc = json.loads(manifest.read_text())
    doc["root_sha256"] = _tree_sha(split)
    manifest.write_text(json.dumps(doc))
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        "count mismatch tasks=0",
        2,
        ["load", "ciphertext", "root"],
    )


def test_split_root_inside_public_evals_is_refused(tmp_path: Path, capsys, monkeypatch, calls) -> None:
    monkeypatch.chdir(tmp_path)
    manifest, ct, _split = _build(tmp_path)
    inside = tmp_path / "evals" / "golden"
    inside.mkdir(parents=True)
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, inside, spy) == heldout.EXIT_REFUSED
    assert calls == []
    assert "reason=split_root_in_repo" in capsys.readouterr().out


@pytest.mark.parametrize("where", ["github_workspace", "repo_root", "cwd"])
def test_split_root_inside_checkout_is_refused(
    tmp_path: Path, capsys, monkeypatch, calls, where: str
) -> None:
    """CTO-rec M2 / P6: a split root anywhere in the checkout is refused before any read."""

    manifest, ct, split = _build(tmp_path)
    env = dict(CLEAN_ENV)
    if where == "github_workspace":
        env["GITHUB_WORKSPACE"] = str(tmp_path)
    elif where == "repo_root":
        monkeypatch.setattr(heldout, "_REPO_ROOT", tmp_path)
    else:
        monkeypatch.chdir(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy(calls), env=env) == heldout.EXIT_REFUSED
    assert calls == []
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=path_in_checkout executed=0" in out.out


def test_staging_and_audit_store_inside_checkout_are_refused(
    tmp_path: Path, capsys, monkeypatch, calls
) -> None:
    manifest, ct, split = _build(tmp_path)
    ws = tmp_path / "ws"
    (ws / "tmp").mkdir(parents=True)
    env = {**CLEAN_ENV, "GITHUB_WORKSPACE": str(ws)}
    monkeypatch.setattr(heldout.tempfile, "tempdir", str(ws / "tmp"))  # TMPDIR inside the checkout
    assert _run(tmp_path, manifest, ct, split, _Spy(calls), env=env) == heldout.EXIT_REFUSED
    assert "reason=path_in_checkout" in capsys.readouterr().out
    monkeypatch.setattr(heldout.tempfile, "tempdir", str(tmp_path / "rt"))
    reason = heldout.location_refusal(env, split_root=split, audit_store=ws / "audit")
    assert reason == "path_in_checkout" and calls == []


def test_staging_outside_runner_temp_is_refused(tmp_path: Path, capsys, monkeypatch, calls) -> None:
    """Under ``env -i`` TMPDIR fell back to /tmp (P1t). With RUNNER_TEMP set, staging must be in it."""

    manifest, ct, split = _build(tmp_path)  # split is tmp_path/ho
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(heldout.tempfile, "tempdir", str(elsewhere))
    env = {**CLEAN_ENV, "RUNNER_TEMP": str(tmp_path)}
    assert _run(tmp_path, manifest, ct, split, _Spy(calls), env=env) == heldout.EXIT_REFUSED
    assert "reason=path_outside_runner_temp executed=0" in capsys.readouterr().out
    rt_tmp = tmp_path / "tmp"
    rt_tmp.mkdir()
    monkeypatch.setattr(heldout.tempfile, "tempdir", str(rt_tmp))
    assert heldout.location_refusal(env, split_root=split, audit_store=tmp_path / "audit") is None
    assert calls == []


def test_staged_copy_lives_under_tmpdir_and_is_removed(
    tmp_path: Path, capsys, monkeypatch, calls, fake_docker
) -> None:
    rt_tmp = tmp_path / "tmp"
    rt_tmp.mkdir()
    monkeypatch.setattr(heldout.tempfile, "tempdir", str(rt_tmp))
    seen: list[Path] = []

    class Where(_Spy):
        def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
            seen.append(fixture)
            return super().__call__(fixture, task_class, run_dir)

    manifest, ct, split = _build(tmp_path)
    env = {**CLEAN_ENV, "RUNNER_TEMP": str(tmp_path)}
    assert _run(tmp_path, manifest, ct, split, Where(calls), env=env) == heldout.EXIT_OK
    assert seen and all(p.resolve().is_relative_to(rt_tmp.resolve()) for p in seen)
    assert list(rt_tmp.iterdir()) == []  # finally: the staged copy is gone
    capsys.readouterr()


# ------------------------------------------------------------------ preflight refusals


@pytest.mark.parametrize(
    "env",
    [{"RUNNER_DEBUG": "1"}, {"ACTIONS_STEP_DEBUG": "true"}, {"ACTIONS_RUNNER_DEBUG": "1"}],
    ids=["runner-debug", "step-debug", "runner-debug-secret"],
)
def test_debug_logging_refuses_before_reading_manifest(tmp_path: Path, capsys, calls, env: dict) -> None:
    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, split, spy, env={**CLEAN_ENV, **env}) == heldout.EXIT_REFUSED
    assert calls == []
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=debug_logging_on executed=0" in out.out
    assert not (tmp_path / "audit").exists()


@pytest.mark.parametrize(
    "env",
    [
        {"RECERTIA_EXECUTION_BACKEND": "local"},
        {"RECERTIA_GOLDEN_BACKEND": "local"},
        {"RECERTIA_GOLDEN_BACKEND": "inherit"},
        {"RECERTIA_API_ALLOW_LOCAL_EXEC": "1"},
    ],
    ids=["exec-local", "golden-local", "golden-inherit", "api-local-exec"],
)
def test_local_backend_env_is_refused(tmp_path: Path, capsys, calls, env: dict) -> None:
    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)
    assert _run(tmp_path, manifest, ct, split, spy, env={**CLEAN_ENV, **env}) == heldout.EXIT_REFUSED
    assert calls == [] and "reason=backend_not_container" in capsys.readouterr().out


def test_provider_env_must_be_empty(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)
    code = _run(tmp_path, manifest, ct, split, spy, env={**CLEAN_ENV, "OPENAI_API_KEY": "x"})
    assert code == heldout.EXIT_REFUSED and calls == []
    assert "reason=provider_env_not_empty" in capsys.readouterr().out


def test_require_offline_refuses_when_network_reachable(tmp_path, capsys, monkeypatch, calls) -> None:  # noqa: ANN001
    manifest, ct, split = _build(tmp_path)
    monkeypatch.setattr(heldout, "network_reachable", lambda: True)
    code = _run(tmp_path, manifest, ct, split, _Spy(calls), require_offline=True, verify_only=True)
    assert code == heldout.EXIT_REFUSED and calls == []
    assert "reason=network_reachable" in capsys.readouterr().out
    monkeypatch.setattr(heldout, "network_reachable", lambda: False)
    code = _run(tmp_path, manifest, ct, split, _Spy(calls), require_offline=True, verify_only=True)
    assert code == heldout.EXIT_OK


def test_network_reachable_reads_interfaces_and_fails_closed(tmp_path: Path) -> None:
    head = "Inter-|   Receive\n face |bytes    packets\n"
    net_dev = tmp_path / "dev"
    assert heldout.network_reachable(net_dev) is True
    net_dev.write_text(head + "    lo:       0       0    0\n")
    assert heldout.network_reachable(net_dev) is False
    net_dev.write_text(head + "    lo:  1 1 0\n  eth0: 9 9 0\n")
    assert heldout.network_reachable(net_dev) is True


# ------------------------------------------------------------------ container preflight (M1)


def test_container_preflight_failure_fails_closed(tmp_path: Path, capsys, calls) -> None:
    from recertia.solver.sandbox import SandboxError

    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)

    def missing_image() -> None:
        raise SandboxError("container probe failed via docker: exit=125 Unable to find image")

    code = _run(tmp_path, manifest, ct, split, spy, preflight_containers=missing_image)
    assert code == heldout.EXIT_REFUSED and "execute" not in calls
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=container_preflight executed=0" in out.out


def _container_settings(monkeypatch: pytest.MonkeyPatch, image: str) -> None:
    monkeypatch.setenv("RECERTIA_CONTAINER_RUNTIME", "docker")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_GOLDEN_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_CONTAINER_IMAGE", image)


DIGEST_IMAGE = "python:3.12-slim@sha256:" + "a" * 64


def test_real_container_preflight_missing_image_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The M1 probe path (container.py:176) with --pull never: a missing image raises."""

    from recertia.solver import container
    from recertia.solver.sandbox import SandboxError

    _container_settings(monkeypatch, DIGEST_IMAGE)
    monkeypatch.setattr(container, "container_runtime", lambda: "docker")
    seen: dict = {}

    def no_image(runtime, command, *, workdir, spec, timeout_s, **kw):  # noqa: ANN001
        seen["image"] = spec.image
        seen["network"] = spec.network
        return subprocess.CompletedProcess(args=[], returncode=125, stdout="", stderr="Unable to find image")

    monkeypatch.setattr(container, "_container_run", no_image)
    with pytest.raises(SandboxError):
        heldout.container_preflight()
    assert seen == {"image": DIGEST_IMAGE, "network": "none"}


def test_real_container_preflight_refuses_tag_only_image_and_no_runtime(monkeypatch) -> None:  # noqa: ANN001
    from recertia.solver import container
    from recertia.solver.sandbox import SandboxError

    _container_settings(monkeypatch, "python:3.12-slim")
    monkeypatch.setattr(container, "container_runtime", lambda: "docker")
    with pytest.raises(SandboxError, match="digest"):
        heldout.container_preflight()
    _container_settings(monkeypatch, DIGEST_IMAGE)
    monkeypatch.setattr(container, "container_runtime", lambda: None)
    with pytest.raises(SandboxError):
        heldout.container_preflight()
    monkeypatch.setenv("RECERTIA_GOLDEN_BACKEND", "local")
    with pytest.raises(SandboxError, match="container backend"):
        heldout.container_preflight()


@pytest.mark.parametrize("runtime", ["podman", None], ids=["podman", "unset"])
def test_container_preflight_requires_docker_runtime(monkeypatch, runtime: str | None) -> None:  # noqa: ANN001
    """CTO-rec N-E: the runtime is pinned to docker; podman or an unpinned runtime is refused."""

    from recertia.solver import container
    from recertia.solver.sandbox import SandboxError

    _container_settings(monkeypatch, DIGEST_IMAGE)
    if runtime is None:
        monkeypatch.delenv("RECERTIA_CONTAINER_RUNTIME")
    else:
        monkeypatch.setenv("RECERTIA_CONTAINER_RUNTIME", runtime)
    monkeypatch.setattr(container, "container_runtime", lambda: runtime or "podman")
    with pytest.raises(SandboxError, match="RUNTIME=docker"):
        heldout.container_preflight()


def test_preflight_refuses_a_non_docker_runtime_env(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    spy = _Spy(calls)
    env = {**CLEAN_ENV, "RECERTIA_CONTAINER_RUNTIME": "podman"}
    assert _run(tmp_path, manifest, ct, split, spy, env=env) == heldout.EXIT_REFUSED
    assert calls == []
    assert "FAIL reason=runtime_not_docker executed=0" in capsys.readouterr().out


def test_digest_image_runs_with_pull_never_and_no_network(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    """Evidence for how containers run: the argv _container_run builds (container.py:264,299-300)."""

    from recertia.solver import container

    captured: dict = {}

    def fake_run(args, **kwargs):  # noqa: ANN001
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(container.subprocess, "run", fake_run)
    spec = container.ContainerSpec(image=DIGEST_IMAGE)
    container._container_run("docker", "true", workdir=tmp_path, spec=spec, timeout_s=5)
    args = captured["args"]
    assert "--network=none" in args
    assert args[args.index("--pull") + 1] == "never"


# ------------------------------------------------------------------ backend_used


def test_local_backend_used_fails(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy(calls, backend="local")) == heldout.EXIT_EVAL_FAILED
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=backend_not_container runs=2" in out.out


def test_no_command_run_counts_as_non_container(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy(calls, backend=None)) == heldout.EXIT_EVAL_FAILED
    assert "reason=backend_not_container runs=2" in capsys.readouterr().out


def test_backend_tracker_restores_launchers() -> None:
    from recertia.solver import container, executor

    before = (container._container_run, container._local_run, executor._container_run)
    with heldout.track_backends([]):
        assert container._container_run is not before[0]
    assert (container._container_run, container._local_run, executor._container_run) == before


# ------------------------------------------------------------------ model clients


def test_constructing_a_model_client_exits_1(tmp_path: Path, capsys, calls, fake_docker) -> None:
    from recertia.solver.model import ModelClient, StubModelClient

    original = ModelClient.__init__
    manifest, ct, split = _build(tmp_path)

    class Builds(_Spy):
        def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
            super().__call__(fixture, task_class, run_dir)
            try:
                StubModelClient(responses=["x"])
            except heldout.ModelClientForbidden:
                pass  # an engine that swallows the error must still fail the run
            return True

    assert _run(tmp_path, manifest, ct, split, Builds(calls)) == 1
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=model_client_constructed count=2" in out.out
    assert ModelClient.__init__ is original


def test_plan_node_lazy_import_is_guarded(tmp_path: Path, capsys, calls, fake_docker, monkeypatch) -> None:
    """nodes/plan.py:108 imports guide_stitch lazily; a client built through it must exit 1."""

    from recertia.solver.model import ModelClient, StubModelClient

    monkeypatch.delitem(sys.modules, "recertia.nodes.guide_stitch", raising=False)
    manifest, ct, split = _build(tmp_path)
    state: dict = {}

    class PlanPath(_Spy):
        def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
            super().__call__(fixture, task_class, run_dir)
            state["guard_on_at_import"] = ModelClient.__init__.__name__ == "_refuse_init"
            import recertia.nodes.guide_stitch as gs  # the plan.py:108 lazy import, under the guard

            monkeypatch.setattr(gs, "stitch_guide", lambda skills: StubModelClient(responses=["x"]))
            from recertia.nodes.guide_stitch import stitch_guide

            try:
                stitch_guide([])
            except heldout.ModelClientForbidden:
                pass
            return True

    assert _run(tmp_path, manifest, ct, split, PlanPath(calls)) == 1
    assert state["guard_on_at_import"] is True
    assert "reason=model_client_constructed" in capsys.readouterr().out


def test_subclass_skipping_super_init_is_still_refused(tmp_path: Path, capsys, calls, fake_docker) -> None:
    """CTO-rec N3 / P8-nosuper: ``__new__`` is guarded, not only ``ModelClient.__init__``."""

    from recertia.solver.model import ModelClient, StubModelClient

    class NoSuper(StubModelClient):
        def __init__(self) -> None:  # deliberately skips super().__init__
            self.x = 1

    manifest, ct, split = _build(tmp_path)

    class Builds(_Spy):
        def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
            super().__call__(fixture, task_class, run_dir)

            class LateNoSuper(ModelClient):  # defined during the run
                def __init__(self) -> None:
                    self.y = 2

                def _complete_once(self, prompt, *, system):  # noqa: ANN001, ANN201
                    raise NotImplementedError

            for make in (NoSuper, LateNoSuper):
                try:
                    make()
                except heldout.ModelClientForbidden:
                    pass
            state.append(LateNoSuper)
            return True

    state: list[type] = []
    original = ModelClient.__init__
    assert _run(tmp_path, manifest, ct, split, Builds(calls)) == 1
    assert "reason=model_client_constructed count=4" in capsys.readouterr().out
    assert ModelClient.__init__ is original and "__init_subclass__" not in ModelClient.__dict__
    assert NoSuper().x == 1 and StubModelClient(responses=["x"]) is not None


@pytest.mark.parametrize("module", ["recertia.solver.providers", "anthropic", "openai.types"])
def test_provider_module_import_is_refused(tmp_path: Path, capsys, calls, fake_docker, module: str) -> None:
    """The guard is in place at provider-module import: importing one during a run exits 1."""

    import importlib

    before = "recertia.solver.providers" in sys.modules
    manifest, ct, split = _build(tmp_path)

    class Imports(_Spy):
        def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
            super().__call__(fixture, task_class, run_dir)
            try:
                importlib.import_module(module)
            except Exception:  # noqa: BLE001 - swallowed, still fails the run
                pass
            return True

    assert _run(tmp_path, manifest, ct, split, Imports(calls)) == 1
    assert "reason=model_client_constructed count=2" in capsys.readouterr().out
    assert ("recertia.solver.providers" in sys.modules) is before  # hidden modules restored
    importlib.import_module("recertia.solver.providers")  # importable again after the run


# ------------------------------------------------------------------ output and scan


def test_fd_level_writes_go_to_audit_store_not_console(tmp_path: Path, capfd, calls, fake_docker) -> None:
    import os

    manifest, ct, split = _build(tmp_path)

    class Raw(_Spy):
        def __call__(self, fixture: Path, task_class: str, run_dir: Path) -> bool:
            super().__call__(fixture, task_class, run_dir)
            os.write(1, f"{fixture.name} {CONTENT_MARK}\n".encode())
            os.write(2, f"{fixture.name}\n".encode())
            return True

    assert _run(tmp_path, manifest, ct, split, Raw(calls)) == heldout.EXIT_OK
    out = capfd.readouterr()
    _assert_no_leak(out.out + out.err)
    (run_dir,) = (tmp_path / "audit").iterdir()
    assert FIXTURE_IDS[0] in (run_dir / "eval.log").read_text()


@pytest.mark.parametrize(
    "leak",
    [FIXTURE_IDS[0], f"repo-chore/{FIXTURE_IDS[1]}/task.json", "golden-some-skill-v1-a1b2c3", "golden_path"],
    ids=["fixture-dir", "fixture-path", "run-id-shape", "golden-path-key"],
)
def test_log_scan_fails_on_fixture_ids(tmp_path: Path, capsys, leak: str) -> None:
    _manifest, _ct, split = _build(tmp_path)
    console = tmp_path / "console.txt"
    console.write_text(f"heldout: PASS passed=2 failed=0\nengine said {leak}\n")
    assert heldout.main(["scan", "--split-root", str(split), "--log", str(console)]) == heldout.EXIT_LEAK
    out = capsys.readouterr()
    assert out.out.startswith("log-scan: FAIL hits=")
    for needle in (*FIXTURE_IDS, "golden-some-skill"):
        assert needle not in out.out + out.err


def test_deny_list_has_skill_ids_request_prefixes_and_split_root(tmp_path: Path) -> None:
    """CTO-rec N2 / proposal §4 item 11, for both the in-run guard and ``scan``."""

    _m, _c, split = _build(tmp_path)
    task = split / "repo-chore" / FIXTURE_IDS[0] / "task.json"
    task.write_text(
        json.dumps(
            {
                "request": "zz-request-text that runs on past forty characters",
                "expected_skill_id": "zz-skill-x",
            }
        )
    )
    terms = heldout.deny_terms(split, [str(split)])
    assert "zz-skill-x" in terms and str(split) in terms
    assert "zz-request-text that runs on past forty" in terms  # first 40 chars
    for leak in ("zz-skill-x", "zz-request-text that runs on past forty", f"path {split}/x"):
        console = tmp_path / "c.txt"
        console.write_text(f"heldout: PASS\n{leak}\n")
        assert heldout.main(["scan", "--split-root", str(split), "--log", str(console)]) == heldout.EXIT_LEAK


def test_scan_drops_terms_that_are_already_public(tmp_path: Path, capsys) -> None:
    """The published log echoes step env (paths) and the workflow; those are not leaks."""

    _m, _c, split = _build(tmp_path)
    log = tmp_path / "published.log"
    log.write_text(f"2026-10-20T00:00:00Z   SPLIT_ROOT: {split}\nheldout: PASS passed=2 failed=0\n")
    public = tmp_path / "public.txt"
    public.write_text(f"{split}\n")
    args = ["scan", "--split-root", str(split), "--log", str(log)]
    assert heldout.main(args) == heldout.EXIT_LEAK
    assert heldout.main([*args, "--public-text", str(public)]) == heldout.EXIT_OK
    log.write_text(log.read_text() + f"oops {FIXTURE_IDS[0]}\n")
    assert heldout.main([*args, "--public-text", str(public)]) == heldout.EXIT_LEAK
    out = capsys.readouterr().out
    assert FIXTURE_IDS[0] not in out and str(split) not in out


def test_log_scan_passes_clean_console(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy(calls), verify_only=True) == heldout.EXIT_OK
    console = tmp_path / "console.txt"
    console.write_text(capsys.readouterr().out)
    assert heldout.main(["scan", "--split-root", str(split), "--log", str(console)]) == heldout.EXIT_OK
    assert heldout.main(["scan", "--split-root", str(tmp_path / "nope"), "--log", str(console)]) == 2


def test_cli_verify_ciphertext_and_quiet_usage(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    base = ["verify-ciphertext", "--manifest", str(manifest), "--ciphertext", str(ct)]
    assert heldout.main(base) == heldout.EXIT_OK
    ct.write_bytes(b"x")
    assert heldout.main(base) == heldout.EXIT_VERIFY_FAILED
    assert heldout.main(["--manifest", str(split / "repo-chore" / FIXTURE_IDS[0])]) == heldout.EXIT_USAGE
    out = capsys.readouterr()
    assert "FAIL hash mismatch stage=ciphertext" in out.out
    for needle in (*FIXTURE_IDS, CONTENT_MARK):
        assert needle not in out.out + out.err


# ------------------------------------------------------------------ workflow


def test_workflow_is_dispatch_only_pinned_container_and_secretless() -> None:
    text = (REPO / ".github/workflows/heldout-operator.yml").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0].rstrip() for line in text.splitlines())
    on_block = code.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  ([a-z_]+):", on_block, flags=re.MULTILINE) == ["workflow_dispatch"]
    assert "type: choice" in on_block and "type: string" not in on_block
    assert "\npermissions: {}\n" in code
    assert re.findall(r"runs-on:\s*(\S+)", code) == ["ubuntu-24.04", "ubuntu-24.04"]
    uses = re.findall(r"uses:\s*(\S+)", code)
    assert uses and all(re.fullmatch(r"actions/(checkout|setup-python)@[0-9a-f]{40}", u) for u in uses), uses
    for banned in (
        "environment:",
        "secrets.",
        "upload-artifact",
        "actions/cache",
        "cache:",
        "GITHUB_STEP_SUMMARY",
        "pull_request",
        "workflow_run",
        "issue_comment",
        "schedule",
        "push:",
        "self-hosted",
        "outputs:",
        "local",
        "evals/golden",
    ):
        assert banned not in code, banned
    assert re.search(r'python-version: "3\.12\.\d+"', code)
    assert "--require-hashes -r requirements/heldout-eval.txt" in code
    assert "TODO(REC-022)" in text and "access-storage proposal" in text
    names = re.findall(r"- name: (.+)", code)
    assert names[:2] == ["Refuse debug logging", "Refuse non-main ref"], names
    order = [
        "Pull allowlisted image by digest",
        "Verify ciphertext sha256",
        "Decrypt into RUNNER_TEMP",
        "Verify root and count, container preflight, evaluate",
    ]
    idx = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
    assert idx == sorted(idx), names
    assert '"$GITHUB_REF" != "refs/heads/main"' in code
    assert "evals/heldout/heldout-repo-chore-v1.manifest.json" in code
    assert re.search(r"RECERTIA_CONTAINER_IMAGE: python:3\.12-slim@sha256:[0-9a-f]{64}", code)
    assert 'RECERTIA_REQUIRE_IMAGE_DIGEST: "1"' in code
    assert "sudo unshare --net" in code and "--require-offline" in code
    assert code.index("heldout scan") < code.index('cat "$CONSOLE"')
    assert 'OPENAI_API_KEY: ""' in code and 'ANTHROPIC_API_KEY: ""' in code
    pull = code.split("Pull allowlisted image by digest", 1)[1].split("- name:", 1)[0]
    assert "docker pull" in pull and "KEY" not in pull and "TOKEN" not in pull
    # Raw text, not comment-stripped: GitHub expands ${{ }} even inside a shell comment in run:
    # (a literal one there made the file invalid). Only column-0 YAML header comments may hold one.
    for line in text.splitlines():
        if "${{" in line and not line.startswith("#"):
            assert re.fullmatch(r"\s+[A-Z_0-9]+: \$\{\{ [a-z_.A-Z]+ \}\}(/[a-z.]+)*", line), line


def _steps(code: str) -> dict[str, str]:
    """``- name:`` -> step body (comments stripped), evaluate job first, then log-scan."""

    out: dict[str, str] = {}
    for job, body in re.findall(r"^  ([a-z-]+):\n(.*?)(?=^  [a-z-]+:\n|\Z)", code, flags=re.M | re.S):
        for name, step in re.findall(r"- name: ([^\n]+)\n(.*?)(?=^      - |\Z)", body, flags=re.M | re.S):
            out[f"{job}/{name}"] = step
    return out


def test_workflow_cto_must_fixes_m2_to_m5_and_log_scan_job() -> None:
    text = (REPO / ".github/workflows/heldout-operator.yml").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0].rstrip() for line in text.splitlines())
    steps = _steps(code)
    evaluate = steps["evaluate/Verify root and count, container preflight, evaluate (offline)"]
    # M3: no new privileges and no supplementary groups at all; the harness reaches docker only
    # through a uid ACL on the socket, set before the netns.
    setpriv = evaluate.split("setpriv", 1)[1].split('"$PY"', 1)[0]
    assert "--no-new-privs" in setpriv and "--clear-groups" in setpriv
    for flag in ("--groups", "--init-groups", "--keep-groups"):
        assert flag not in setpriv.replace("--clear-groups", ""), flag
    assert "--init-groups" not in code and "--keep-groups" not in code
    acl = 'sudo setfacl -m "u:$(id -u):rw" "$SOCK"'
    assert acl in evaluate and evaluate.index(acl) < evaluate.index("sudo unshare --net")
    assert "docker_socket_or_setfacl_missing" in evaluate
    # N-E: runtime pinned to docker for the job and through env -i.
    assert "RECERTIA_CONTAINER_RUNTIME: docker" in code and "RECERTIA_CONTAINER_RUNTIME=docker" in evaluate
    # N-D: the always() wipe revokes the socket ACL.
    assert 'sudo setfacl -x "u:$(id -u)" "$SOCK"' in steps["evaluate/Wipe held-out material"]
    # M2 / N4: staging under RUNNER_TEMP; the in-module guards see the workspace and debug flag.
    for passed in (
        'TMPDIR="$HO_TMP"',
        'RUNNER_TEMP="$RUNNER_TEMP"',
        'GITHUB_WORKSPACE="$GITHUB_WORKSPACE"',
        'RUNNER_DEBUG="${RUNNER_DEBUG:-}"',
    ):
        assert passed in evaluate, passed
    assert "HO_TMP: ${{ runner.temp }}/tmp" in evaluate
    # M2 / N5: key files never outlive the decrypt step, and the last step always wipes all.
    for job in ("evaluate", "log-scan"):
        names = [k for k in steps if k.startswith(job + "/")]
        wipe = steps[names[-1]]
        assert names[-1] == f"{job}/Wipe held-out material", names
        assert "if: always()" in wipe and "*.key" in wipe
        for d in ('"$RT/ho"', '"$RT/tmp"', '"$RT/keys"', '"$RT/ct"'):
            assert d in wipe, (job, d)
        decrypt = next(
            v for k, v in steps.items() if k.startswith(job + "/") and "ecrypt" in k and "PENDING" in k
        )
        assert "trap 'rm -rf \"$KEY_DIR\"' EXIT" in decrypt
        # M4: a decrypt step that is no longer the fail-closed stub must emit ::add-mask::.
        stub = "decrypt_not_approved" in decrypt and "exit 1" in decrypt
        assert stub or "::add-mask::" in decrypt
    assert "proposal §4 item 8: RIGHT AFTER" in text and "::add-mask::" in text
    # M5: no deployment environment yet; the TODO says when it is added.
    assert not re.search(r"^\s*environment:", code, flags=re.M)
    assert "`environment: heldout-operator` is added to BOTH jobs in the same" in text
    # N7: the published-log scan job.
    log_scan = code.split("\n  log-scan:\n", 1)[1]
    assert "needs: evaluate" in log_scan and "if: always()" in log_scan.split("steps:", 1)[0]
    assert "actions: read" in log_scan and "contents: read" in log_scan
    assert (
        "actions/jobs/$JOB_ID/logs" in log_scan and "heldout scan" in log_scan and "--public-text" in log_scan
    )
    jobs_perms = re.findall(r"^    permissions:\n((?:      .+\n)+)", code + "\n", flags=re.M)
    assert all("write" not in p for p in jobs_perms) and len(jobs_perms) == 2


def test_audit_record_goes_to_the_owners_two_recipients_only() -> None:
    """The audit record is encrypted to the owner's key and recovery key only.

    No separate operator recipient. The job key stays where it is (the decrypt step), and the
    workflow names recipients only, never key material.
    """

    text = (REPO / ".github/workflows/heldout-operator.yml").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0].rstrip() for line in text.splitlines())
    persist = _steps(code)["evaluate/Persist to audit store (PENDING approval; fails closed)"]
    assert "audit_sink_not_approved" in persist and "exit 1" in persist
    raw = text.split("- name: Persist to audit store", 1)[1].split("\n      - name:", 1)[0]
    flat = " ".join(raw.replace("#", " ").split())
    assert "HELDOUT_AUDIT_RECIPIENTS: the owner's key and recovery key" in flat
    assert "recipients only" in raw
    assert not re.search(r"\bthe\s+operator\b", raw, flags=re.I), "no separate operator recipient"
    assert "age1" not in text and "AGE-SECRET-KEY" not in text and "BEGIN " not in text


# ------------------------------------------------------------------ main's real WO-06 messages

_SAMPLE = {
    "unknown": "1",
    "missing": "0",
    "HASH_RULE": repr(wo06.HASH_RULE),
    "key": "root_sha256",
    "kind": "hash",
    "manifest.root_sha256": "a" * 64,
    "actual_root": "b" * 64,
    "manifest.count": "5",
    "actual_count": "4",
}


def _wo06_messages() -> set[str]:
    """Every HeldoutManifestError message in main's module, formatted ones with sample values."""

    tree = ast.parse((REPO / "src/recertia/evals/heldout_manifest.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "HeldoutManifestError"):
            continue
        (arg,) = node.args
        if isinstance(arg, ast.Constant):
            assert isinstance(arg.value, str)
            found.add(arg.value)
            continue
        assert isinstance(arg, ast.JoinedStr)
        parts: list[str] = []
        for value in arg.values:
            if isinstance(value, ast.Constant):
                assert isinstance(value.value, str)
                parts.append(value.value)
            else:
                assert isinstance(value, ast.FormattedValue)
                parts.append(_SAMPLE[ast.unparse(value.value)])
        found.add("".join(parts))
    return found


def test_every_wo06_message_passes_unredacted() -> None:
    """Lucas-rec #2: all 30 of main's loader messages are on the allowlist, verbatim."""

    messages = _wo06_messages()
    assert len(messages) == 30, sorted(messages)
    for message in messages:
        assert heldout.loader_reason(HeldoutManifestError(message)) == message, message
    assert heldout.loader_reason(HeldoutManifestError("manifest refused: count must be ")) == (
        heldout.LOADER_REDACTED
    )


def _empty_dir(split: Path) -> None:
    (split / "repo-chore" / "empty").mkdir()


def _not_nfc(split: Path) -> None:
    (split / "repo-chore" / "cafe\u0301.json").write_text("{}")


def _tamper(split: Path) -> None:
    (split / "SELECTION.md").write_text("changed")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_empty_dir, "held-out tree refused: empty directory"),
        (_not_nfc, "held-out tree refused: name is not NFC"),
        (_tamper, "held-out hash mismatch: manifest root_sha256="),
    ],
    ids=["empty-dir", "not-nfc", "hash"],
)
def test_real_verifier_messages_are_visible_end_to_end(
    tmp_path: Path, capsys, calls, mutate, message: str
) -> None:
    """Lucas-rec #2 / #3: driven through main's real verify_heldout_tree, its message is printed."""

    manifest, ct, split = _build(tmp_path)
    mutate(split)
    assert _run(tmp_path, manifest, ct, split, _Spy(calls)) == heldout.EXIT_VERIFY_FAILED
    assert calls == ["load", "ciphertext", "root"]
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    (line,) = out.out.splitlines()
    assert line.startswith(f"heldout: FAIL {message}") and line.endswith(" executed=0"), line
    assert "manifest_error redacted" not in out.out


def test_real_verifier_split_id_refusal_is_visible(tmp_path: Path, capsys, calls) -> None:
    """A manifest the runner's own reader accepts but WO-06 refuses: WO-06's message is printed."""

    manifest, ct, split = _build(tmp_path, split_id="Not-A-Split-Id")
    _expect_fail(
        tmp_path,
        capsys,
        manifest,
        ct,
        split,
        calls,
        "manifest refused: split_id format",
        2,
        ["load", "ciphertext", "root"],
    )


def test_real_verifier_message_with_anything_extra_is_redacted(
    tmp_path: Path, capsys, monkeypatch, calls
) -> None:
    """Anything that is not exactly one of main's messages stays redacted, even from the real type."""

    def refuse(manifest_path: Path, root: Path) -> HeldoutManifest:
        raise HeldoutManifestError("held-out tree refused: special file repo-chore/zz-secret-fixture-alpha")

    monkeypatch.setattr(wo06, "verify_heldout_tree", refuse)
    manifest, ct, split = _build(tmp_path)
    assert _run(tmp_path, manifest, ct, split, _Spy([])) == heldout.EXIT_VERIFY_FAILED
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "heldout: FAIL manifest_error redacted executed=0" in out.out
