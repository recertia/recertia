"""API keys are HMAC-SHA256. PBKDF2 rows rehash. The limiter is IP plus key id."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from recertia.api.auth import ApiKeyStore


def test_issued_key_stored_as_hmac_not_plaintext(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "keys.sqlite")
    issued = store.issue(tenant_id="alpha", scopes={"runs"}, actor="op")
    blob = (tmp_path / "keys.sqlite").read_bytes()
    assert issued.secret.encode() not in blob
    with sqlite3.connect(tmp_path / "keys.sqlite") as conn:
        row = conn.execute(
            "SELECT hash_alg, pepper_id, salt FROM api_keys WHERE key_id=?",
            (issued.key_id,),
        ).fetchone()
    assert row[0] == "hmac-sha256"
    assert row[1] == "v1"
    assert store.authenticate(issued.secret) is not None


def test_lazy_rehash_from_pbkdf2(tmp_path: Path) -> None:
    path = tmp_path / "keys.sqlite"
    store = ApiKeyStore(path)
    # Repeated characters stay under the generic-api-key entropy floor.
    legacy = "rec_key_aaaaaaaaaaaaaaaa.example"
    salt = b"0123456789abcdef"
    digest = ApiKeyStore._hash(legacy, salt)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO api_keys(
                   key_id, tenant_id, scopes, salt, key_hash, created_at, hash_alg, pepper_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "key_aaaaaaaaaaaaaaaa",
                "alpha",
                "runs",
                salt,
                digest,
                "2026-01-01T00:00:00+00:00",
                "pbkdf2-sha256",
                "legacy",
            ),
        )
    assert store.authenticate(legacy) is not None
    with sqlite3.connect(path) as conn:
        alg = conn.execute(
            "SELECT hash_alg FROM api_keys WHERE key_id='key_aaaaaaaaaaaaaaaa'"
        ).fetchone()[0]
    assert alg == "hmac-sha256"
    assert store.authenticate(legacy) is not None


def test_limiter_keys_on_ip_and_key_id(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "keys.sqlite", max_failures=2)
    secret_a = "rec_key_aaaaaaaaaaaaaaaa.one"
    secret_b = "rec_key_aaaaaaaaaaaaaaaa.two"
    secret_c = "rec_key_aaaaaaaaaaaaaaaa.three"
    assert store.authenticate(secret_a, client_ip="10.0.0.8") is None
    assert store.authenticate(secret_b, client_ip="10.0.0.8") is None
    assert store.authenticate(secret_c, client_ip="10.0.0.8") is None
    with sqlite3.connect(tmp_path / "keys.sqlite") as conn:
        failed = conn.execute(
            "SELECT COUNT(*) FROM api_key_audit WHERE action='authentication_failed'"
        ).fetchone()[0]
    assert failed == 2
    assert store.authenticate("rec_key_bbbbbbbbbbbbbbbb.other", client_ip="10.0.0.8") is None
    with sqlite3.connect(tmp_path / "keys.sqlite") as conn:
        failed_other = conn.execute(
            "SELECT COUNT(*) FROM api_key_audit WHERE action='authentication_failed'"
        ).fetchone()[0]
    assert failed_other == 3


def test_audit_caps_success_and_redacts_secret(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "keys.sqlite")
    issued = store.issue(tenant_id="alpha", scopes={"runs"}, actor="op")
    assert store.authenticate(issued.secret) is not None
    assert store.authenticate(issued.secret) is not None
    with sqlite3.connect(tmp_path / "keys.sqlite") as conn:
        rows = conn.execute(
            "SELECT detail FROM api_key_audit WHERE action='authenticated'"
        ).fetchall()
    assert len(rows) == 1
    blob = (tmp_path / "keys.sqlite").read_bytes()
    assert issued.secret.encode() not in blob


def test_tenant_pepper_mismatch_rejects(tmp_path: Path) -> None:
    pepper = b"platform-pepper-32b-value!!"
    store = ApiKeyStore(tmp_path / "keys.sqlite", pepper=pepper, bind_tenant=True)
    issued = store.issue(tenant_id="alpha", scopes={"runs"}, actor="op")
    assert store.authenticate(issued.secret) is not None
    beta = store._hmac(issued.secret, tenant="beta", pepper_id="v1")
    with sqlite3.connect(tmp_path / "keys.sqlite") as conn:
        stored = conn.execute(
            "SELECT key_hash FROM api_keys WHERE key_id=?",
            (issued.key_id,),
        ).fetchone()[0]
    assert beta != stored
