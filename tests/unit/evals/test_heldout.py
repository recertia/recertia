"""REC-022: the held-out runner fails closed and never prints fixture ids or contents."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from recertia.evals import heldout

REPO = Path(__file__).resolve().parents[3]

FIXTURE_IDS = ("zz-secret-fixture-alpha", "zz-secret-fixture-bravo")
CONTENT_MARK = "zz-private-content-marker"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build(tmp_path: Path) -> tuple[Path, Path, str]:
    split = tmp_path / "split"
    files = []
    for fid in FIXTURE_IDS:
        for name, body in (
            ("task.json", json.dumps({"request": f"{CONTENT_MARK} {fid}"})),
            ("expect.json", json.dumps({"terminal": "solved"})),
        ):
            path = split / fid / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            files.append({"path": f"{fid}/{name}", "sha256": _sha(path.read_bytes())})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": heldout.MANIFEST_SCHEMA, "task_class": "repo-chore", "files": files}),
        encoding="utf-8",
    )
    return manifest, split, _sha(manifest.read_bytes())


class _Spy:
    def __init__(self) -> None:
        self.calls = 0
        self.saw: list[str] = []

    def __call__(self, fixtures: list[Path], task_class: str, run_dir: Path) -> tuple[int, int]:
        self.calls += 1
        self.saw = sorted(p.name for p in fixtures)
        # Engine output that would leak ids must land in the audit store, not stdout/stderr.
        print(f"engine log {CONTENT_MARK} {' '.join(self.saw)}")
        return len(fixtures), 0


def _run(tmp_path: Path, manifest: Path, split: Path, expected: str, spy: _Spy, **kw: object) -> int:
    return heldout.run(
        manifest_path=manifest,
        expected_sha256=expected,
        split_root=split,
        audit_store=tmp_path / "audit",
        executor=spy,
        env=kw.pop("env", {}),
        **kw,  # type: ignore[arg-type]
    )


def _assert_no_leak(text: str) -> None:
    for needle in (*FIXTURE_IDS, CONTENT_MARK, "task.json", "expect.json", "zz-"):
        assert needle not in text, needle
    for line in text.splitlines():
        assert line.startswith("heldout: "), line
        assert re.fullmatch(r"heldout: (PASS|FAIL|VERIFIED)( [a-z_0-9-]+(=[a-z_0-9]+)?)*", line), line


def test_good_manifest_runs_executor_on_staged_copy(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    spy = _Spy()
    assert _run(tmp_path, manifest, split, expected, spy) == heldout.EXIT_OK
    assert spy.calls == 1 and spy.saw == sorted(FIXTURE_IDS)
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert f"manifest_sha256={expected}" in out.out
    assert "files=4 fixtures=2" in out.out
    (run_dir,) = (tmp_path / "audit").iterdir()
    assert json.loads((run_dir / "record.json").read_text())["status"] == "evaluated"
    assert CONTENT_MARK in (run_dir / "eval.log").read_text()


def test_verify_only_does_not_execute(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    spy = _Spy()
    assert _run(tmp_path, manifest, split, expected, spy, verify_only=True) == heldout.EXIT_OK
    assert spy.calls == 0
    _assert_no_leak("".join(capsys.readouterr()))


def _expect_fail(tmp_path: Path, capsys, manifest: Path, split: Path, expected: str, reason: str) -> None:
    spy = _Spy()
    code = _run(tmp_path, manifest, split, expected, spy)
    assert code == heldout.EXIT_VERIFY_FAILED
    assert spy.calls == 0, "executor ran despite a failed verification"
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert f"reason={reason}" in out.out and "executed=0" in out.out


def test_bad_manifest_hash_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, _ = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, split, "0" * 64, "manifest_hash_mismatch")


def test_edited_manifest_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    _expect_fail(tmp_path, capsys, manifest, split, expected, "manifest_hash_mismatch")


def test_malformed_expected_sha_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, _ = _build(tmp_path)
    _expect_fail(tmp_path, capsys, manifest, split, "not-a-hash", "expected_sha256_malformed")


def test_tampered_input_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    (split / FIXTURE_IDS[1] / "task.json").write_text("tampered", encoding="utf-8")
    _expect_fail(tmp_path, capsys, manifest, split, expected, "input_hash_mismatch")


def test_missing_input_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    (split / FIXTURE_IDS[0] / "expect.json").unlink()
    _expect_fail(tmp_path, capsys, manifest, split, expected, "input_missing")


def test_unlisted_input_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    (split / FIXTURE_IDS[0] / "extra.json").write_text("{}", encoding="utf-8")
    _expect_fail(tmp_path, capsys, manifest, split, expected, "input_unlisted")


def test_symlinked_input_fails_closed(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    target = split / FIXTURE_IDS[0] / "task.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)
    _expect_fail(tmp_path, capsys, manifest, split, expected, "input_hash_mismatch")


@pytest.mark.parametrize(
    "bad_path",
    ["../escape/task.json", "/abs/task.json", "top-level.json"],
    ids=["dotdot", "absolute", "top-level"],
)
def test_unsafe_manifest_paths_fail_closed(tmp_path: Path, capsys, bad_path: str) -> None:
    manifest, split, _ = _build(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["files"][0]["path"] = bad_path
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    _expect_fail(tmp_path, capsys, manifest, split, _sha(manifest.read_bytes()), "manifest_bad_entries")


def test_cli_exit_codes_and_quiet_usage(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    base = ["--manifest", str(manifest), "--split-root", str(split), "--audit-store", str(tmp_path / "a")]
    assert heldout.main([*base, "--expected-sha256", expected, "--verify-only"]) == heldout.EXIT_OK
    assert heldout.main([*base, "--expected-sha256", "f" * 64]) == heldout.EXIT_VERIFY_FAILED
    assert heldout.main(["--manifest", str(split / FIXTURE_IDS[0])]) == heldout.EXIT_USAGE
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)


def test_workflow_is_dispatch_only_pinned_and_secretless() -> None:
    text = (REPO / ".github/workflows/heldout-operator.yml").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0].rstrip() for line in text.splitlines())
    on_block = code.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    triggers = re.findall(r"^  ([a-z_]+):", on_block, flags=re.MULTILINE)
    assert triggers == ["workflow_dispatch"]
    assert "type: choice" in on_block and "type: string" not in on_block
    assert "\npermissions: {}\n" in code
    assert re.findall(r"runs-on:\s*(\S+)", code) == ["ubuntu-24.04"]
    uses = re.findall(r"uses:\s*(\S+)", code)
    assert uses and all(re.fullmatch(r"actions/(checkout|setup-python)@[0-9a-f]{40}", u) for u in uses), uses
    for banned in (
        "environment:", "secrets.", "upload-artifact", "actions/cache", "cache:", "GITHUB_STEP_SUMMARY",
        "pull_request", "workflow_run", "issue_comment", "schedule", "push:", "self-hosted", "outputs:",
    ):
        assert banned not in code, banned
    assert re.search(r'python-version: "3\.12\.\d+"', code)
    assert "--require-hashes -r requirements/heldout-eval.txt" in code
    assert "TODO(REC-022)" in text and "Harper-rec" in text
    # Guards come first, in this order, before checkout.
    names = re.findall(r"- name: (.+)", code)
    assert names[:2] == ["Refuse debug logging", "Refuse non-main ref"], names
    assert code.index("Refuse non-main ref") < code.index("actions/checkout@")
    assert '"$GITHUB_REF" != "refs/heads/main"' in code
    assert "${{ runner.debug }}" in code and "RUNNER_DEBUG" in code
    # Offline eval, captured console, scanned before it is printed.
    assert "sudo unshare --net" in code and "--require-offline" in code
    assert code.index("heldout scan") < code.index('cat "$CONSOLE"')
    # WO-08 Amendment 1: provider env empty.
    assert 'OPENAI_API_KEY: ""' in code and 'ANTHROPIC_API_KEY: ""' in code
    # Context values reach scripts only through env, never by interpolation into run blocks.
    for line in code.splitlines():
        if "${{" in line:
            assert re.fullmatch(r"\s+[A-Z_0-9]+: \$\{\{ [a-z_.A-Z]+ \}\}(/[a-z.]+)?", line), line

@pytest.mark.parametrize(
    "env",
    [{"RUNNER_DEBUG": "1"}, {"ACTIONS_STEP_DEBUG": "true"}, {"ACTIONS_RUNNER_DEBUG": "1"}],
)
def test_debug_logging_refuses_before_reading_manifest(tmp_path: Path, capsys, env: dict) -> None:
    manifest, split, expected = _build(tmp_path)
    spy = _Spy()
    assert _run(tmp_path, manifest, split, expected, spy, env=env) == heldout.EXIT_REFUSED
    assert spy.calls == 0
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=debug_logging_on executed=0" in out.out
    assert not (tmp_path / "audit").exists(), "refusal must happen before any audit write"


def test_provider_env_must_be_empty(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    spy = _Spy()
    code = _run(tmp_path, manifest, split, expected, spy, env={"OPENAI_API_KEY": "x"})
    assert code == heldout.EXIT_REFUSED and spy.calls == 0
    assert "reason=provider_env_not_empty" in capsys.readouterr().out


def test_require_offline_refuses_when_network_reachable(tmp_path: Path, capsys, monkeypatch) -> None:
    manifest, split, expected = _build(tmp_path)
    spy = _Spy()
    monkeypatch.setattr(heldout, "network_reachable", lambda: True)
    assert _run(tmp_path, manifest, split, expected, spy, require_offline=True) == heldout.EXIT_REFUSED
    assert spy.calls == 0 and "reason=network_reachable" in capsys.readouterr().out
    monkeypatch.setattr(heldout, "network_reachable", lambda: False)
    assert _run(tmp_path, manifest, split, expected, spy, require_offline=True) == heldout.EXIT_OK


def test_constructing_a_model_client_exits_1(tmp_path: Path, capsys) -> None:
    from recertia.solver.model import ModelClient, StubModelClient

    original = ModelClient.__init__
    manifest, split, expected = _build(tmp_path)

    def builds_client(fixtures: list[Path], task_class: str, run_dir: Path) -> tuple[int, int]:
        try:
            StubModelClient(responses=["x"])
        except heldout.ModelClientForbidden:
            pass  # an engine that swallows the error must still fail the run
        return len(fixtures), 0

    code = heldout.run(
        manifest_path=manifest,
        expected_sha256=expected,
        split_root=split,
        audit_store=tmp_path / "audit",
        executor=builds_client,
        env={},
    )
    assert code == 1
    out = capsys.readouterr()
    _assert_no_leak(out.out + out.err)
    assert "reason=model_client_constructed count=1" in out.out
    assert ModelClient.__init__ is original, "guard must restore the constructor"
    StubModelClient(responses=["x"])  # usable again outside the held-out run


def test_golden_only_run_passes_with_no_model_client(tmp_path: Path, capfd, monkeypatch) -> None:
    """Real golden executor on a synthetic split copied from a PUBLIC fixture (local backend)."""

    import shutil

    monkeypatch.chdir(REPO)
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_GOLDEN_BACKEND", "local")
    split = tmp_path / "split"
    shutil.copytree(REPO / "evals/golden/repo-chore/add-editorconfig", split / "add-editorconfig")
    files = [
        {"path": p.relative_to(split).as_posix(), "sha256": _sha(p.read_bytes())}
        for p in sorted(split.rglob("*"))
        if p.is_file()
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": heldout.MANIFEST_SCHEMA, "task_class": "repo-chore", "files": files})
    )
    code = heldout.run(
        manifest_path=manifest,
        expected_sha256=_sha(manifest.read_bytes()),
        split_root=split,
        audit_store=tmp_path / "audit",
        env={},
    )
    out = capfd.readouterr()
    assert code == heldout.EXIT_OK, out.out
    assert "PASS passed=1 failed=0" in out.out
    assert "add-editorconfig" not in out.out + out.err
    (run_dir,) = (tmp_path / "audit").iterdir()
    record = json.loads((run_dir / "record.json").read_text())
    assert record["status"] == "evaluated" and "model_clients" not in record


def test_fd_level_writes_go_to_audit_store_not_console(tmp_path: Path, capfd) -> None:
    import os

    manifest, split, expected = _build(tmp_path)

    def raw_writer(fixtures: list[Path], task_class: str, run_dir: Path) -> tuple[int, int]:
        os.write(1, f"{FIXTURE_IDS[0]} {CONTENT_MARK}\n".encode())
        os.write(2, f"{FIXTURE_IDS[1]}\n".encode())
        return len(fixtures), 0

    code = heldout.run(
        manifest_path=manifest, expected_sha256=expected, split_root=split,
        audit_store=tmp_path / "audit", executor=raw_writer, env={},
    )
    assert code == heldout.EXIT_OK
    out = capfd.readouterr()
    _assert_no_leak(out.out + out.err)
    (run_dir,) = (tmp_path / "audit").iterdir()
    assert FIXTURE_IDS[0] in (run_dir / "eval.log").read_text()


@pytest.mark.parametrize(
    "leak",
    [FIXTURE_IDS[0], f"{FIXTURE_IDS[1]}/task.json", "golden-some-skill-v1-a1b2c3", "golden_path"],
    ids=["fixture-dir", "fixture-path", "run-id-shape", "golden-path-key"],  # opaque ids in -v logs
)
def test_log_scan_fails_on_fixture_ids(tmp_path: Path, capsys, leak: str) -> None:
    manifest, _split, expected = _build(tmp_path)
    console = tmp_path / "console.txt"
    console.write_text(f"heldout: PASS passed=2 failed=0\nengine said {leak}\n", encoding="utf-8")
    args = ["scan", "--manifest", str(manifest), "--expected-sha256", expected, "--log", str(console)]
    code = heldout.main(args)
    assert code == heldout.EXIT_LEAK
    out = capsys.readouterr()
    assert out.out.startswith("log-scan: FAIL hits=")
    for needle in (*FIXTURE_IDS, "golden-some-skill"):
        assert needle not in out.out + out.err


def test_log_scan_passes_clean_console_and_rejects_bad_manifest(tmp_path: Path, capsys) -> None:
    manifest, split, expected = _build(tmp_path)
    spy = _Spy()
    assert _run(tmp_path, manifest, split, expected, spy) == heldout.EXIT_OK
    console = tmp_path / "console.txt"
    console.write_text(capsys.readouterr().out, encoding="utf-8")
    args = ["scan", "--manifest", str(manifest), "--log", str(console)]
    assert heldout.main([*args, "--expected-sha256", expected]) == heldout.EXIT_OK
    assert heldout.main([*args, "--expected-sha256", "0" * 64]) == heldout.EXIT_VERIFY_FAILED
    assert "log-scan: PASS hits=0 logs=1" in capsys.readouterr().out


def test_network_reachable_reads_interfaces_and_fails_closed(tmp_path: Path) -> None:
    head = "Inter-|   Receive\n face |bytes    packets\n"
    net_dev = tmp_path / "dev"
    assert heldout.network_reachable(net_dev) is True  # unreadable -> reachable (fail closed)
    net_dev.write_text(head + "    lo:       0       0    0\n")
    assert heldout.network_reachable(net_dev) is False  # loopback only: offline namespace
    net_dev.write_text(head + "    lo:  1 1 0\n  eth0: 9 9 0\n")
    assert heldout.network_reachable(net_dev) is True
    net_dev.write_text(head)
    assert heldout.network_reachable(net_dev) is True
