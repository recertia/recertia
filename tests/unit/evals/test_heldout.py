"""REC-022: the held-out runner fails closed, in spec order, and never prints ids or contents.

The WO-06 loader (``load_heldout_manifest`` / ``compute_root_sha256``) is not on main yet.
These tests install a minimal test double under ``heldout.LOADER_MODULE``. It is a double,
not an implementation for production use.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import types
import unicodedata
from pathlib import Path

import pytest

from recertia.evals import heldout

REPO = Path(__file__).resolve().parents[3]
FIXTURE_IDS = ("zz-secret-fixture-alpha", "zz-secret-fixture-bravo")
CONTENT_MARK = "zz-private-content-marker"
CLEAN_ENV = {"RECERTIA_EXECUTION_BACKEND": "container", "RECERTIA_GOLDEN_BACKEND": "container"}
_KEYS = {
    "schema_version", "split_id", "count", "hash_rule", "root_sha256", "ciphertext_sha256", "created_at_pt",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _double_root(root: Path) -> str:
    lines = []
    for p in root.rglob("*"):
        if p.is_symlink() or not (p.is_file() or p.is_dir()):
            raise ValueError("special file")
        if p.is_file():
            rel = unicodedata.normalize("NFC", p.relative_to(root).as_posix())
            lines.append((rel.encode(), f"{_sha(p.read_bytes())}  {rel}\n".encode()))
    return _sha(b"".join(line for _, line in sorted(lines)))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install the loader test double and record call order."""

    order: list[str] = []
    mod = types.ModuleType(heldout.LOADER_MODULE)

    def load_heldout_manifest(path: Path) -> dict:
        order.append("load")
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if set(doc) != _KEYS or doc["schema_version"] != 1:
            raise ValueError("unknown or missing keys")
        return doc

    def compute_root_sha256(root: Path) -> str:
        order.append("root")
        return _double_root(Path(root))

    mod.load_heldout_manifest = load_heldout_manifest  # type: ignore[attr-defined]
    mod.compute_root_sha256 = compute_root_sha256  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, heldout.LOADER_MODULE, mod)
    real_ct = heldout.verify_ciphertext

    def ct(manifest, path):  # noqa: ANN001
        order.append("ciphertext")
        return real_ct(manifest, path)

    monkeypatch.setattr(heldout, "verify_ciphertext", ct)
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
        "count": len(FIXTURE_IDS),
        "hash_rule": heldout.HASH_RULE,
        "root_sha256": _double_root(split),
        "ciphertext_sha256": _sha(ciphertext.read_bytes()),
        "created_at_pt": "2026-10-20T14:00:00-07:00",
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
            container._container_run("docker", "true", workdir=run_dir, spec=None, timeout_s=1)
        elif self.backend == "local":
            try:
                container._local_run("true", workdir=run_dir)
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
    assert "count=2" in out.out and "PASS passed=2 failed=0" in out.out
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
        tmp_path, capsys, manifest, ct, split, calls, "hash mismatch stage=ciphertext", 2,
        ["load", "ciphertext"],
    )


def test_missing_ciphertext_fails_closed(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    ct.unlink()
    _expect_fail(
        tmp_path, capsys, manifest, ct, split, calls, "hash mismatch stage=ciphertext", 2,
        ["load", "ciphertext"],
    )


def test_tampered_file_root_mismatch(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    (split / "repo-chore" / FIXTURE_IDS[1] / "task.json").write_text("tampered")
    _expect_fail(
        tmp_path, capsys, manifest, ct, split, calls, "hash mismatch stage=root", 2,
        ["load", "ciphertext", "root"],
    )


def test_missing_and_extra_files_root_mismatch(tmp_path: Path, capsys, calls) -> None:
    manifest, ct, split = _build(tmp_path)
    (split / "repo-chore" / FIXTURE_IDS[0] / "expect.json").unlink()
    (split / "extra.txt").write_text("x")
    _expect_fail(
        tmp_path, capsys, manifest, ct, split, calls, "hash mismatch stage=root", 2,
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
    _expect_fail(
        tmp_path, capsys, manifest, ct, split, calls, "count mismatch expected=3 got=2", 2,
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
            state["guard_on_at_import"] = ModelClient.__init__.__name__ == "_refuse"
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
    assert re.findall(r"runs-on:\s*(\S+)", code) == ["ubuntu-24.04"]
    uses = re.findall(r"uses:\s*(\S+)", code)
    assert uses and all(re.fullmatch(r"actions/(checkout|setup-python)@[0-9a-f]{40}", u) for u in uses), uses
    for banned in (
        "environment:", "secrets.", "upload-artifact", "actions/cache", "cache:", "GITHUB_STEP_SUMMARY",
        "pull_request", "workflow_run", "issue_comment", "schedule", "push:", "self-hosted", "outputs:",
        "local", "evals/golden",
    ):
        assert banned not in code, banned
    assert re.search(r'python-version: "3\.12\.\d+"', code)
    assert "--require-hashes -r requirements/heldout-eval.txt" in code
    assert "TODO(REC-022)" in text and "Harper-rec" in text
    names = re.findall(r"- name: (.+)", code)
    assert names[:2] == ["Refuse debug logging", "Refuse non-main ref"], names
    order = [
        "Pull allowlisted image by digest", "Verify ciphertext sha256", "Decrypt into RUNNER_TEMP",
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
    for line in code.splitlines():
        if "${{" in line:
            assert re.fullmatch(r"\s+[A-Z_0-9]+: \$\{\{ [a-z_.A-Z]+ \}\}(/[a-z.]+)*", line), line
