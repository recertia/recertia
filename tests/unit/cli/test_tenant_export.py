"""REL-11: tenant export --dry-run lists paths and writes nothing."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from recertia.cli.main import app

runner = CliRunner()


def test_tenant_export_dry_run_lists_tenant_paths(tmp_path: Path) -> None:
    root = tmp_path / ".recertia"
    note = root / "runs" / "acme" / "note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("keep\n", encoding="utf-8")
    blob = root / "blobs" / "acme" / "obj"
    blob.parent.mkdir(parents=True)
    blob.write_text("b", encoding="utf-8")

    listed = runner.invoke(
        app,
        ["tenant", "export", "--tenant", "acme", "--root", str(root), "--dry-run"],
    )
    assert listed.exit_code == 0, listed.output
    assert str(note) in listed.output
    assert str(blob) in listed.output
    assert note.read_text(encoding="utf-8") == "keep\n"

    refused = runner.invoke(app, ["tenant", "export", "--tenant", "acme", "--root", str(root)])
    assert refused.exit_code == 2
    assert "dry-run" in refused.output

    escaped = runner.invoke(
        app, ["tenant", "export", "--tenant", "../acme", "--root", str(root), "--dry-run"]
    )
    assert escaped.exit_code == 2
