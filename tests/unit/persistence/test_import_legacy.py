"""REL-3: import checkpoints.db / operations.db into runtime.db."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import TaskCriterion
from contracts.run import RunManifest, RunState, Task
from recertia.graph.ops import OperationLedger
from recertia.graph.persistence import open_run_persistence
from recertia.graph.store import CheckpointStore
from recertia.persistence.db import MigrationChecksumError
from recertia.persistence.import_legacy import (
    IMPORT_ID,
    import_legacy_runtime,
    legacy_sqlite_pending,
)
from recertia.persistence.layout import RuntimeLayout
from recertia.persistence.root_lock import RootBusy, RootLock, RootLockBusy


def _state(run_id: str, terminal: str | None = None) -> RunState:
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="x",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="h"),
        criteria=[TaskCriterion(id="c", kind="command", run="true", source="caller")],
        criteria_locked_at=datetime.now(timezone.utc),
        terminal=terminal,
    )


def test_import_copies_rows_drops_pending_and_renames(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", "retrieve", _state("run-a", terminal="solved"))
    finally:
        store.close()
    ops = OperationLedger(layout.operations_db)
    try:
        ops.put("run-a", 1, "solve", 0, {"ok": True})
        ops._conn.execute(
            "INSERT INTO operations (run_id, incarnation, attempt_no, node, op_seq, result_json) "
            "VALUES (?, 1, 1, 'solve', 1, ?)",
            ("run-a", json.dumps({"__pending__": True})),
        )
        ops._conn.commit()
    finally:
        ops.close()

    assert import_legacy_runtime(layout) is True
    assert layout.checkpoints_db.is_file()
    assert layout.operations_db.is_file()
    assert (tmp_path / "checkpoints.db.migrated").is_file()
    assert (tmp_path / "operations.db.migrated").is_file()

    conn = sqlite3.connect(layout.runtime_db)
    try:
        ckpt = conn.execute(
            "SELECT run_id, seq, node FROM checkpoints"
        ).fetchall()
        assert ckpt == [("run-a", 0, "intake")]
        op_rows = conn.execute(
            "SELECT op_seq, result_json FROM operations ORDER BY op_seq"
        ).fetchall()
        assert len(op_rows) == 1
        assert op_rows[0][0] == 0
        assert json.loads(op_rows[0][1]) == {"ok": True}
        recorded = conn.execute(
            "SELECT version FROM schema_migrations WHERE version=?",
            (IMPORT_ID,),
        ).fetchone()
        assert recorded is not None
        run_row = conn.execute("SELECT status FROM runs WHERE run_id=?", ("run-a",)).fetchone()
        assert run_row == ("solved",)
    finally:
        conn.close()

    assert import_legacy_runtime(layout) is True


def test_unequal_checkpoint_keeps_dest_and_imports_other_seqs(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    dest = CheckpointStore(layout.runtime_db)
    src = CheckpointStore(layout.checkpoints_db)
    try:
        dest.save("run-a", 0, "intake", "retrieve", _state("run-a"))
        src.save("run-a", 0, "intake", "plan", _state("run-a"))
        src.save("run-a", 1, "plan", None, _state("run-a"))
    finally:
        dest.close()
        src.close()

    assert import_legacy_runtime(layout) is True
    store = CheckpointStore(layout.runtime_db)
    try:
        assert store.latest_seq("run-a") == 1
        seq0 = store.history("run-a")[0]
        assert seq0[1] == "intake"
        assert seq0[2] == "retrieve"
    finally:
        store.close()
    assert layout.checkpoints_db.is_file()
    assert (tmp_path / "checkpoints.db.migrated").is_file()


def test_equal_checkpoint_is_kept_once(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    state = _state("run-a")
    dest = CheckpointStore(layout.runtime_db)
    src = CheckpointStore(layout.checkpoints_db)
    try:
        dest.save("run-a", 0, "intake", "retrieve", state)
        src.save("run-a", 0, "intake", "retrieve", state)
    finally:
        dest.close()
        src.close()

    assert import_legacy_runtime(layout) is True
    conn = sqlite3.connect(layout.runtime_db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
        assert rows == (1,)
    finally:
        conn.close()
    assert layout.checkpoints_db.is_file()
    assert (tmp_path / "checkpoints.db.migrated").is_file()


def test_root_lock_busy_fails_import(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    held = RootLock(layout.parent.root_lock)
    held.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="cannot import leftover runtime files"):
            import_legacy_runtime(layout, timeout_s=0)
        assert layout.checkpoints_db.is_file()
        assert not (tmp_path / "runtime.db").exists()
    finally:
        held.release()


def _open_in_other_thread(root: Path) -> BaseException | None:
    box: list[BaseException] = []

    def run() -> None:
        try:
            opened = open_run_persistence(root)
            opened.close()
        except BaseException as exc:  # noqa: BLE001
            box.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=8)
    return box[0] if box else None


def test_lock_busy_open_raises_without_writing_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *different* thread must not write a ghost runtime.db while exclusive is held."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootLockBusy)
        assert "cannot import leftover runtime files" in str(err)
        assert not (root / "runtime.db").exists()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        assert imported.checkpoints.list_run_ids() == ["old-run"]
        assert imported.checkpoints.latest_seq("old-run") == 1
    finally:
        imported.close()


def test_exclusive_held_by_this_thread_is_removed() -> None:
    """REC-056b1: the pid/tid holder helper was a stub that always said False. It is gone.

    The lock is the flock alone, so nothing may ask the lock file who holds it.
    """

    with pytest.raises(ImportError, match="exclusive_held_by_this_thread"):
        from recertia.persistence.root_lock import exclusive_held_by_this_thread  # type: ignore[attr-defined]

        del exclusive_held_by_this_thread


def test_same_thread_holder_gets_root_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No same-thread exception: opening while this thread holds exclusive is RootBusy."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("old-run", 1, "n", None, _state("old-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        with pytest.raises(RootBusy, match="cannot import leftover runtime files"):
            open_run_persistence(root)
        assert not (root / "runtime.db").exists()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        assert imported.checkpoints.list_run_ids() == ["old-run"]
    finally:
        imported.close()


def test_sigkill_holder_leaves_empty_lock_file(tmp_path: Path) -> None:
    import subprocess
    import sys

    lock_path = tmp_path / ".lock"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from pathlib import Path\n"
            "from recertia.persistence.root_lock import RootLock\n"
            f"l = RootLock(Path({str(lock_path)!r}))\n"
            "l.try_exclusive()\n"
            "os.kill(os.getpid(), 9)\n",
        ],
        check=False,
    )
    assert lock_path.is_file()
    assert lock_path.read_text() == ""


def test_migrate_off_after_import_sees_post_import_writes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    legacy = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            legacy.checkpoints.save("legacy-run", seq, "n", None, _state("legacy-run"))
    finally:
        legacy.close()
    imported = open_run_persistence(root)
    try:
        imported.checkpoints.save("legacy-run", 4, "post-import", None, _state("legacy-run"))
    finally:
        imported.close()
    off = open_run_persistence(root, migrate="off")
    try:
        assert off.checkpoints.latest_seq("legacy-run") == 4
        assert off.checkpoints._db_path == root / "runtime.db"
    finally:
        off.close()


def test_migrate_off_after_import_still_sees_state(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    legacy = open_run_persistence(root, migrate="off")
    try:
        for i in range(5):
            legacy.checkpoints.save("legacy", i + 1, "n", None, _state("legacy"))
        legacy.operations.put("legacy", 1, "n", 0, {"v": 1})
    finally:
        legacy.close()
    open_run_persistence(root).close()
    off = open_run_persistence(root, migrate="off")
    try:
        assert off.checkpoints.latest_seq("legacy") == 5
    finally:
        off.close()


def test_import_ops_rebuild_resumable_after_rel3_leftover(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path).local_runs()
    from recertia.persistence import import_legacy as il

    conn = sqlite3.connect(layout.runtime_db)
    conn.execute(
        "CREATE TABLE operations ("
        "run_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, node TEXT NOT NULL, "
        "op_seq INTEGER NOT NULL, result_json TEXT NOT NULL, "
        "PRIMARY KEY (run_id, attempt_no, node, op_seq))"
    )
    conn.execute("INSERT INTO operations VALUES ('old', 1, 'n', 0, '{\"v\":1}')")
    conn.execute(il._LIVE_OPERATIONS_DDL.replace("operations", "operations_rel3", 1))
    conn.commit()
    conn.close()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("old", 1, "n", None, _state("old"))
    finally:
        store.close()
    assert import_legacy_runtime(layout) is True
    ops = OperationLedger(layout.runtime_db)
    try:
        applied, result = ops.get("old", 1, "n", 0)
        assert applied is True
        assert result == {"v": 1}
    finally:
        ops.close()


def test_open_run_persistence_auto_uses_runtime_db(tmp_path: Path) -> None:
    persistence = open_run_persistence(tmp_path / "runs")
    try:
        persistence.checkpoints.save("run-a", 0, "intake", None, _state("run-a"))
        assert persistence.checkpoints._db_path == tmp_path / "runs" / "runtime.db"
        assert persistence.operations._db_path == tmp_path / "runs" / "runtime.db"
    finally:
        persistence.close()
    assert (tmp_path / "runs" / "runtime.db").is_file()
    assert not (tmp_path / "runs" / "checkpoints.db").exists()


def test_open_run_persistence_off_keeps_legacy_files(tmp_path: Path) -> None:
    persistence = open_run_persistence(tmp_path / "runs", migrate="off")
    try:
        persistence.checkpoints.save("run-a", 0, "intake", None, _state("run-a"))
        assert persistence.checkpoints._db_path == tmp_path / "runs" / "checkpoints.db"
        assert persistence.operations._db_path == tmp_path / "runs" / "operations.db"
    finally:
        persistence.close()
    assert (tmp_path / "runs" / "checkpoints.db").is_file()
    assert not (tmp_path / "runs" / "runtime.db").exists()


def test_open_run_persistence_manual_refuses_legacy(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runs").local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    with pytest.raises(MigrationChecksumError, match="RECERTIA_RUNTIME_DB_MIGRATE=auto"):
        open_run_persistence(tmp_path / "runs", migrate="manual")
    assert layout.checkpoints_db.is_file()


def _registered_cli_path(words: list[str]) -> list[str] | None:
    """Walk ``words`` through the real CLI; return the matched leaf path, or None.

    Groups are recognised by their ``commands`` mapping rather than by class,
    because typer may vendor its own click classes.
    """
    import typer

    from recertia.cli.main import app

    cmd: object = typer.main.get_command(app)
    path: list[str] = []
    for word in words:
        commands = getattr(cmd, "commands", None)
        if not isinstance(commands, dict):
            break  # a leaf command; the rest are its arguments
        sub = commands.get(word)
        if sub is None:
            return None
        cmd = sub
        path.append(word)
    is_group = isinstance(getattr(cmd, "commands", None), dict)
    return path if path and not is_group else None


_MANUAL_CONDITION = "only once no process of an older release uses this root"
_REPO = Path(__file__).resolve().parents[3]


def _release_ids_in(text: str) -> list[str]:
    """Ticket ids, issue numbers, commit hashes and version tags found in ``text``."""
    import re

    patterns = (
        r"\b(?:REC|REL|ADR)-\d+\b",  # ticket ids
        r"(?<![\w&])#\d+\b",  # issue or pull request numbers: #172
        r"\b(?:pr|pull request|issue|gh)[-_ ]?#?\d+\b",  # PR 172, pull request 172, issue 9
        # Commit hashes, short or full: 6 to 40 hex digits with at least one digit and one
        # letter (so "defaced" and "300000" are not hashes), or 12+ letters a-f alone.
        r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,40}\b",
        r"\b[a-f]{12,40}\b",
        r"\bv\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?\b",  # version tags: v3, v1.2, v1.2.3
        r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?\b",  # three-part versions: 1.2.3
        r"\b(?:19|20)\d{2}\.\d{1,2}\b",  # calendar versions: 2026.10 (not "0.3 s")
        r"\b(?:release|rel|tag)[-_ ]?v?\d[\w.-]*\b",  # release-2026.10, tag_3
    )
    return [m.group(0) for pattern in patterns for m in re.finditer(pattern, text, re.IGNORECASE)]


def test_release_id_detector_finds_hashes_tags_and_ticket_ids() -> None:
    for sample in (
        "0e85d983",
        "0e85d983a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "since v1.2.3 only",
        "v3",
        "v1.2",
        "1.2.3",
        "2026.10",
        "0e85d9",
        "a1b2c3",
        "deadbeefcafe",
        "PR 172",
        "pr172",
        "pull request 172",
        "issue 9",
        "release 2026.10",
        "release-2026.10",
        "REC-070",
        "rel-3",
        "REL-3",
        "see #172",
    ):
        assert _release_ids_in(f"reopen with auto ({sample})"), sample
    for innocent in (
        "waits 0.3 s",
        "at most 2.5 s",
        "defaced",
        "facade",
        "decade",
        "beaded",
        "300000",
        "1000000 ms",
        "root_busy",
        "the 5 s default",
    ):
        assert _release_ids_in(f"reopen with auto ({innocent})") == [], innocent
    clean = (
        "only once no process of an older release uses this root, set"
        " RECERTIA_RUNTIME_DB_MIGRATE=auto (the default) and reopen; retry on root_busy"
    )
    assert _release_ids_in(clean) == []


def test_manual_mode_error_names_only_registered_cli_commands(tmp_path: Path) -> None:
    """#172: the `manual` refusal must not send operators to a missing command.

    Every ``recertia <subcommand ...>`` named in the message or in
    ``details["action"]`` must resolve to a registered CLI leaf command. Every
    ``RECERTIA_RUNTIME_DB_*=value`` it names must round-trip through the settings:
    the key is read (a non-default probe changes the setting) and the value is
    accepted. The condition "only once no process of an older release uses this root"
    comes before the advice (an older ``migrate=off`` writer would lose writes to the
    import) and both texts say to stay on ``off`` otherwise; ``details["action"]``
    says to retry on ``root_busy``. Neither text names a ticket id, issue number,
    commit hash, version tag or path. The procedure must actually import the pending
    files.
    """
    import re

    from recertia.settings import load_settings

    layout = RuntimeLayout(tmp_path / "runs").local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    with pytest.raises(MigrationChecksumError) as excinfo:
        open_run_persistence(tmp_path / "runs", migrate="manual")
    action = excinfo.value.details["action"]
    assert isinstance(action, str)
    texts = [str(excinfo.value), action]

    # The walker accepts real commands and rejects missing or partial ones.
    assert _registered_cli_path(["migrate", "run-events"]) == ["migrate", "run-events"]
    assert _registered_cli_path(["migrate", "apply"]) is None
    assert _registered_cli_path(["migrate"]) is None

    # Case-insensitive, and not the env names (``RECERTIA_...``): no word character or
    # underscore may follow ``recertia``.
    command = re.compile(r"(?<![\w-])recertia(?![\w-])((?:\s+[a-z][a-z0-9_-]*)+)", re.IGNORECASE)
    named = [m.group(1).lower().split() for t in texts for m in command.finditer(t)]
    for words in named:
        assert _registered_cli_path(words) is not None, (
            f"names unregistered command: recertia {' '.join(words)}"
        )
    capitalised = [m.group(1).lower().split() for m in command.finditer("run Recertia migrate apply")]
    assert capitalised == [["migrate", "apply"]]  # the extraction sees a capitalised command
    assert not list(command.finditer("RECERTIA_RUNTIME_DB_MIGRATE=auto"))

    # Every named setting is read by the settings and its value is accepted. The message
    # and the action stay in step: the same settings, in the same order.
    per_text = [re.findall(r"\b(RECERTIA_RUNTIME_DB_[A-Z_]+)=([^\s;,)]+)", t) for t in texts]
    assert per_text[0] == per_text[1]
    assignments = per_text[0]
    assert [value for _key, value in assignments] == ["auto", "off"]  # the advice, then the fallback
    assert len({key for key, _value in assignments}) == 1
    for key, value in assignments:
        assert load_settings({key: "manual"}).runtime_db_migrate == "manual"  # key is read
        assert load_settings({key: value}).runtime_db_migrate == value  # value accepted
    assert load_settings({}).runtime_db_migrate == "auto"  # "(the default)" is true

    # The condition leads the advice, the fallback is to stay on `off`, and the action
    # says to retry. No ticket id, issue number, commit hash, version tag or path.
    assert _release_ids_in(_MANUAL_CONDITION) == []
    for text in texts:
        low = text.lower()
        assert _MANUAL_CONDITION in low
        assert low.index(_MANUAL_CONDITION) < low.index("reopen")
        assert low.index(_MANUAL_CONDITION) < low.index("recertia_runtime_db_migrate=auto")
        assert low.index("recertia_runtime_db_migrate=auto") < low.index("otherwise")
        assert low.index("otherwise") < low.index("recertia_runtime_db_migrate=off")
        assert "stay on" in low
        assert _release_ids_in(text) == [], text
        assert not re.search(r"[\\/]", text), text
    assert "retry on root_busy" in action

    # Following the named procedure imports the pending files; `manual` then opens.
    persistence = open_run_persistence(tmp_path / "runs", migrate="auto")
    persistence.close()
    assert not legacy_sqlite_pending(layout)
    persistence = open_run_persistence(tmp_path / "runs", migrate="manual")
    try:
        assert persistence.checkpoints.latest("run-a") is not None
    finally:
        persistence.close()


def _doc_segment(doc: str, start: str, end: str) -> str:
    assert doc.count(start) == 1, start
    begin = doc.index(start)
    stop = doc.index(end, begin + len(start))
    return doc[begin:stop].lower()


def test_manual_mode_advice_is_pinned_in_changelog_and_rel_specs() -> None:
    """The docs give the same advice as the runtime text, so the contradiction (a doc
    saying to reopen with ``auto`` without the condition) cannot come back by a docs-only
    edit. Each place that gives the advice must carry the condition before the ``auto``
    reopen, and say to stay on ``off`` otherwise; the places that describe the whole
    procedure also carry the ``root_busy`` retry."""
    import re

    def norm(path: str) -> str:
        return " ".join((_REPO / path).read_text(encoding="utf-8").split())

    changelog = norm("CHANGELOG.md")
    specs = norm("docs/plans/remediation/rel-specs.md")
    auto = "recertia_runtime_db_migrate=auto"
    stay_env = "stay on `recertia_runtime_db_migrate=off`"
    # (place, segment, the advice, how it says to stay on `off`, whether it says to retry)
    places = [
        (
            "CHANGELOG REC-070 entry",
            _doc_segment(changelog, "**`manual` mode named a command that does not exist", " - **"),
            auto,
            stay_env,
            True,
        ),
        (
            "CHANGELOG mixed-version bullet",
            _doc_segment(changelog, "If you cannot stop every process at once", " Tests: `tests/unit/"),
            auto,
            "keep the new processes on `migrate=off`",
            False,
        ),
        (
            "CHANGELOG REL-3 sentence",
            _doc_segment(changelog, "**REL-3 runtime.db**", " - **REL-3 FencedRunPersistence**"),
            "reopen with `recertia_runtime_db_migrate=auto`",
            stay_env,
            True,
        ),
        (
            "rel-specs manual row",
            _doc_segment(specs, "| `manual` | Refuse to start.", " | `off` |"),
            auto,
            stay_env,
            True,
        ),
        (
            "rel-specs status paragraph",
            _doc_segment(specs, "Status (#172): these `migrate`", " `RECERTIA_RUNTIME_DB_MIGRATE`:"),
            "the `auto` open",
            "stay on `off`",
            False,
        ),
        (
            "rel-specs large-import bullet",
            _doc_segment(specs, "- Importing a large `checkpoints.db`", " - "),
            "the `auto` open",
            "stay on `off`",
            False,
        ),
    ]
    for name, segment, advice, stay, needs_retry in places:
        assert _MANUAL_CONDITION in segment, name
        assert segment.index(_MANUAL_CONDITION) < segment.index(advice), name  # condition leads
        assert stay in segment, name  # stay on `off` otherwise
        if needs_retry:
            assert re.search(r"retry on `?root_busy|on `?root_busy`?, retry", segment), name


def test_migrate_off_leftover_open_wait_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REC-064 (#153): ``migrate=off`` with pending leftover files waits for the root lock
    at most ``RECERTIA_IMPORT_LOCK_TIMEOUT_S`` and then raises typed ``RootBusy``, writing
    nothing. Before, this path waited without a bound. The open runs in a thread so a
    missing bound fails the test instead of hanging it; the bound is the env value (0.3 s),
    well under the 5 s default, so a read of the wrong variable fails too."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.3")
    root = tmp_path / "runs"
    layout = RuntimeLayout(root).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    outcome: list[tuple[BaseException | None, float]] = []

    def attempt() -> None:
        started = time.monotonic()
        try:
            open_run_persistence(root, migrate="off").close()
            error: BaseException | None = None
        except BaseException as exc:  # noqa: BLE001
            error = exc
        outcome.append((error, time.monotonic() - started))

    held = RootLock(layout.parent.root_lock)
    held.try_exclusive()
    worker = threading.Thread(target=attempt, daemon=True)
    try:
        worker.start()
        worker.join(timeout=2.5)
        finished = not worker.is_alive()
    finally:
        held.release()
        worker.join(timeout=15)
    assert finished, "the migrate=off open is still waiting: its wait is not bounded"
    ((error, elapsed),) = outcome
    assert isinstance(error, RootBusy)
    assert isinstance(error.__cause__, RootLockBusy)
    assert 0.25 <= elapsed <= 2.0  # not a single try, and not the 5 s default
    assert layout.checkpoints_db.is_file()
    assert not layout.runtime_db.exists()  # nothing was written elsewhere
    # After the holder is gone, the retry opens the leftover files and sees the data.
    persistence = open_run_persistence(root, migrate="off")
    try:
        assert persistence.checkpoints.latest("run-a") is not None
    finally:
        persistence.close()


def test_migrate_off_leftover_open_wait_ends_when_the_holder_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``migrate=off`` wait polls: when the holder leaves mid-wait, the open succeeds
    soon after, not at the end of the bound. A wait that sleeps the whole bound and then
    tries once passes the test above (the holder stays) but fails this one: the bound is
    6 s, the holder leaves after 0.2 s, and the open must finish well inside the bound."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "6")
    root = tmp_path / "runs"
    layout = RuntimeLayout(root).local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", None, _state("run-a"))
    finally:
        store.close()
    waiting = threading.Event()
    outcome: list[tuple[object, float]] = []

    def attempt() -> None:
        started = time.monotonic()
        waiting.set()
        result: object
        try:
            result = open_run_persistence(root, migrate="off")
        except BaseException as exc:
            result = exc
        outcome.append((result, time.monotonic() - started))

    held = RootLock(layout.parent.root_lock)
    held.try_exclusive()
    worker = threading.Thread(target=attempt, daemon=True)
    try:
        worker.start()
        assert waiting.wait(timeout=10)
        time.sleep(0.2)
    finally:
        held.release()
        worker.join(timeout=15)
    assert not worker.is_alive()
    ((result, elapsed),) = outcome
    assert not isinstance(result, BaseException), result
    persistence = result
    try:
        assert persistence.checkpoints.latest("run-a") is not None  # type: ignore[attr-defined]
    finally:
        persistence.close()  # type: ignore[attr-defined]
    assert 0.1 <= elapsed < 3.0  # it did wait, and it did not wait out the 6 s bound


def test_open_run_persistence_auto_imports_then_reads(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runs").local_runs()
    store = CheckpointStore(layout.checkpoints_db)
    try:
        store.save("run-a", 0, "intake", "retrieve", _state("run-a"))
    finally:
        store.close()
    persistence = open_run_persistence(tmp_path / "runs", migrate="auto")
    try:
        latest = persistence.checkpoints.latest("run-a")
        assert latest is not None
        assert latest[1] == "intake"
        assert layout.checkpoints_db.is_file()
    finally:
        persistence.close()


def test_lock_busy_open_then_import_keeps_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other-thread busy open fails closed; after unlock, import still sees leftover seqs."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert "cannot import leftover runtime files" in str(err)
        assert not (root / "runtime.db").exists()
    finally:
        lock.release()
    imported = open_run_persistence(root)
    try:
        assert imported.checkpoints.latest_seq("legacy-run") == 3
    finally:
        imported.close()


def test_legacy_writer_holds_root_lock_against_import(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    writer = open_run_persistence(root, migrate="off")
    try:
        writer.checkpoints.save("legacy", 1, "n", None, _state("legacy"))
        with pytest.raises(RootLockBusy):
            import_legacy_runtime(RuntimeLayout(root).local_runs(), timeout_s=0)
    finally:
        writer.close()
    assert import_legacy_runtime(RuntimeLayout(root).local_runs()) is True


def test_legacy_not_pending_after_completed_import(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    layout = RuntimeLayout(root).local_runs()
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("legacy", 1, "n", None, _state("legacy"))
    finally:
        p0.close()
    open_run_persistence(root).close()
    assert layout.checkpoints_db.is_file()
    assert legacy_sqlite_pending(layout) is False


def _db_byte_snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if not (
            name.endswith(".db")
            or name.endswith(".sqlite")
            or name.endswith("-wal")
            or name.endswith("-shm")
            or ".db." in name
        ):
            continue
        snap[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


def _legacy_row_snapshot(root: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    ck_rows: list[object] = []
    op_rows: list[object] = []
    ck = root / "checkpoints.db"
    op = root / "operations.db"
    if ck.is_file():
        conn = sqlite3.connect(ck)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
            ).fetchone():
                ck_rows = conn.execute(
                    "SELECT run_id, seq, node FROM checkpoints ORDER BY run_id, seq"
                ).fetchall()
        finally:
            conn.close()
    if op.is_file():
        conn = sqlite3.connect(op)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
            ).fetchone():
                op_rows = conn.execute(
                    "SELECT run_id, attempt_no, node, op_seq, result_json "
                    "FROM operations ORDER BY run_id, op_seq"
                ).fetchall()
        finally:
            conn.close()
    return tuple(ck_rows), tuple(op_rows)


def test_busy_open_raises_root_busy_without_mutating_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy open raises typed RootBusy and writes nothing during the busy window."""

    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    effects: list[str] = []
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
        p0.operations.run_once(
            "legacy-run", 1, "solve", 0, lambda: effects.append("legacy") or {"v": 1}
        )
    finally:
        p0.close()
    before_bytes = _db_byte_snapshot(root)
    before_rows = _legacy_row_snapshot(root)
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    started = time.monotonic()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert "cannot import leftover runtime files" in str(err)
        assert time.monotonic() - started < 1.5
        assert _db_byte_snapshot(root) == before_bytes
        assert _legacy_row_snapshot(root) == before_rows
        assert not (root / "runtime.db").exists()
        assert effects == ["legacy"]
    finally:
        lock.release()


def test_busy_open_does_not_rerun_legacy_op_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    effects: list[str] = []
    p0 = open_run_persistence(root, migrate="off")
    try:
        p0.checkpoints.save("legacy-run", 1, "n", None, _state("legacy-run"))
        p0.operations.run_once(
            "legacy-run", 1, "solve", 0, lambda: effects.append("legacy") or {"v": 1}
        )
    finally:
        p0.close()
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert effects == ["legacy"]
    finally:
        lock.release()
    opened = open_run_persistence(root)
    try:
        opened.operations.run_once(
            "legacy-run", 1, "solve", 0, lambda: effects.append("busy-rerun") or {"v": 2}
        )
        assert effects == ["legacy"]
        assert opened.checkpoints.latest_seq("legacy-run") == 1
    finally:
        opened.close()


def test_after_busy_release_import_sees_holder_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "0.25")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
    finally:
        p0.close()
    before_bytes = _db_byte_snapshot(root)
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    try:
        err = _open_in_other_thread(root)
        assert isinstance(err, RootBusy)
        assert _db_byte_snapshot(root) == before_bytes
        holder = CheckpointStore(root / "checkpoints.db")
        try:
            holder.save("legacy-run", 4, "holder", None, _state("legacy-run"))
        finally:
            holder.close()
    finally:
        lock.release()
    opened = open_run_persistence(root)
    try:
        assert opened.checkpoints.latest_seq("legacy-run") == 4
        nodes = [node for _seq, node, *_rest in opened.checkpoints.history("legacy-run")]
        assert nodes == ["n1", "n2", "n3", "holder"]
    finally:
        opened.close()


def test_busy_open_waits_then_succeeds_after_holder_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECERTIA_IMPORT_LOCK_TIMEOUT_S", "2")
    root = tmp_path / "runs"
    p0 = open_run_persistence(root, migrate="off")
    try:
        for seq in (1, 2, 3):
            p0.checkpoints.save("legacy-run", seq, f"n{seq}", None, _state("legacy-run"))
    finally:
        p0.close()
    before_bytes = _db_byte_snapshot(root)
    lock = RootLock(root / ".lock")
    lock.try_exclusive()
    errors: list[BaseException] = []
    seen: dict[str, int | None] = {}

    def opener() -> None:
        try:
            opened = open_run_persistence(root)
            seen["seq"] = opened.checkpoints.latest_seq("legacy-run")
            opened.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=opener)
    worker.start()
    time.sleep(0.25)
    assert worker.is_alive()
    assert _db_byte_snapshot(root) == before_bytes
    holder = CheckpointStore(root / "checkpoints.db")
    try:
        holder.save("legacy-run", 4, "holder", None, _state("legacy-run"))
    finally:
        holder.close()
    lock.release()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert errors == []
    assert seen.get("seq") == 4


def test_api_presence_lock_does_not_block_legacy_import(tmp_path: Path) -> None:
    """N: create_app must not hold root/.lock for the process lifetime."""

    from recertia.api import create_app

    root = tmp_path / "api-root"
    layout = RuntimeLayout(root).tenant("t1")
    p0 = open_run_persistence(layout, migrate="off")
    try:
        p0.checkpoints.save("legacy-t1", 1, "n", None, _state("legacy-t1"))
    finally:
        p0.close()
    app = create_app(root=root)
    assert Path(app.state.root_lock.path) == root / "api.lock"
    opened = open_run_persistence(layout)
    try:
        assert opened.checkpoints.latest_seq("legacy-t1") == 1
    finally:
        opened.close()
