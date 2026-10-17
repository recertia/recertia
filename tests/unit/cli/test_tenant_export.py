"""REL-11: tenant export lists paths by default and copies files with --execute."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from recertia.cli.main import app

runner = CliRunner()


def _seed(root: Path) -> tuple[Path, Path]:
    note = root / "runs" / "acme" / "note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("keep\n", encoding="utf-8")
    blob = root / "blobs" / "acme" / "obj"
    blob.parent.mkdir(parents=True)
    blob.write_text("b", encoding="utf-8")
    return note, blob


def test_tenant_export_dry_run_lists_tenant_paths(tmp_path: Path) -> None:
    root = tmp_path / ".recertia"
    note, blob = _seed(root)

    listed = runner.invoke(
        app,
        ["tenant", "export", "--tenant", "acme", "--root", str(root), "--dry-run"],
    )
    assert listed.exit_code == 0, listed.output
    assert str(note) in listed.output
    assert str(blob) in listed.output
    assert note.read_text(encoding="utf-8") == "keep\n"

    default = runner.invoke(app, ["tenant", "export", "--tenant", "acme", "--root", str(root)])
    assert default.exit_code == 0, default.output
    assert str(note) in default.output
    assert note.read_text(encoding="utf-8") == "keep\n"
    assert not (root / "exports").exists()

    escaped = runner.invoke(
        app, ["tenant", "export", "--tenant", "../acme", "--root", str(root), "--dry-run"]
    )
    assert escaped.exit_code == 2


def test_tenant_export_execute_copies_files(tmp_path: Path) -> None:
    root = tmp_path / ".recertia"
    note, blob = _seed(root)
    dest = root / "exports" / "acme"

    copied = runner.invoke(
        app,
        [
            "tenant",
            "export",
            "--tenant",
            "acme",
            "--root",
            str(root),
            "--execute",
            "--dest",
            str(dest),
        ],
    )
    assert copied.exit_code == 0, copied.output
    assert (dest / "runs" / "acme" / "note.txt").read_text(encoding="utf-8") == "keep\n"
    assert (dest / "blobs" / "acme" / "obj").read_text(encoding="utf-8") == "b"
    assert "copied 2 files" in copied.output
    assert note.read_text(encoding="utf-8") == "keep\n"
    assert blob.read_text(encoding="utf-8") == "b"

    staged = tmp_path / "stage"
    via_tmp = runner.invoke(
        app,
        [
            "tenant",
            "export",
            "--tenant",
            "acme",
            "--root",
            str(root),
            "--execute",
            "--dest",
            str(staged),
        ],
    )
    assert via_tmp.exit_code == 0, via_tmp.output
    assert (staged / "blobs" / "acme" / "obj").read_text(encoding="utf-8") == "b"


def test_tenant_export_execute_refuses_unsafe_dest(tmp_path: Path) -> None:
    root = tmp_path / ".recertia"
    note, _blob = _seed(root)

    missing = runner.invoke(
        app,
        ["tenant", "export", "--tenant", "acme", "--root", str(root), "--execute"],
    )
    assert missing.exit_code == 2
    assert "--dest" in missing.output

    outside = runner.invoke(
        app,
        [
            "tenant",
            "export",
            "--tenant",
            "acme",
            "--root",
            str(root),
            "--execute",
            "--dest",
            "/workspace/tenant-export-out",
        ],
    )
    assert outside.exit_code == 2
    assert "dest must be under" in outside.output
    assert not Path("/workspace/tenant-export-out").exists()

    inside = runner.invoke(
        app,
        [
            "tenant",
            "export",
            "--tenant",
            "acme",
            "--root",
            str(root),
            "--execute",
            "--dest",
            str(note.parent / "out"),
        ],
    )
    assert inside.exit_code == 2
    assert "source tenant" in inside.output
    assert not (note.parent / "out").exists()
    assert note.read_text(encoding="utf-8") == "keep\n"
