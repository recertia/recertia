"""Seed version files are HMAC'd when the ledger key is set."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.memory.procedural.seeds.factories import add_editorconfig
from recertia.memory.procedural.seeds.signature import SIG_NAME, SeedSignatureError
from recertia.memory.procedural.store import SkillStore


def test_seed_is_signed_and_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_LEDGER_HMAC_KEY", "seed-key")
    store = SkillStore(tmp_path / "skills")
    version = add_editorconfig()
    dest = store.write_version(version)
    assert (dest.parent / SIG_NAME).is_file()
    loaded = store.get_version(version.skill_id, version.version)
    assert loaded.skill_id == version.skill_id

    dest.write_text(dest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(SeedSignatureError, match="does not verify"):
        store.get_version(version.skill_id, version.version)


def test_unsigned_seed_loads_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_LEDGER_HMAC_KEY", raising=False)
    store = SkillStore(tmp_path / "skills")
    version = add_editorconfig()
    dest = store.write_version(version)
    assert not (dest.parent / SIG_NAME).exists()
    assert store.get_version(version.skill_id, version.version).skill_id == version.skill_id
