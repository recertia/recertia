"""Settings load and deployment validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from recertia.errors import ConfigError, ExecutionUnavailable
from recertia.settings import load_settings


def test_api_default_async_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_API_DEFAULT_ASYNC", raising=False)
    assert load_settings().api_default_async is False


def test_api_sync_wait_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECERTIA_API_SYNC_WAIT_S", raising=False)
    assert load_settings().api_sync_wait_s == 60.0
    monkeypatch.setenv("RECERTIA_API_SYNC_WAIT_S", "12.5")
    assert load_settings().api_sync_wait_s == 12.5
    monkeypatch.setenv("RECERTIA_API_SYNC_WAIT_S", "nope")
    with pytest.raises(ConfigError, match="RECERTIA_API_SYNC_WAIT_S"):
        load_settings()


def test_bool_flags_accept_1_true_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_API_ALLOW_LOCAL_EXEC", "yes")
    monkeypatch.setenv("RECERTIA_API_DEFAULT_ASYNC", "true")
    monkeypatch.setenv("RECERTIA_ALLOW_FAULTS", "1")
    settings = load_settings()
    assert settings.api_allow_local_exec is True
    assert settings.api_default_async is True
    assert settings.allow_faults is True


def test_database_url_alias_must_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://a")
    monkeypatch.setenv("DATABASE_URL", "postgres://b")
    with pytest.raises(ConfigError):
        load_settings()


def test_single_node_accepts_local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "single_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    settings = load_settings()
    settings.validate_deployment(role="api")
    assert settings.execution_backend == "local"


def test_multi_node_refuses_local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("RECERTIA_DB_BACKEND", "postgres")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://db")
    monkeypatch.setenv("RECERTIA_BLOB_STORE", "s3")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("RECERTIA_API_KEY_PEPPER", "pepper")
    with pytest.raises(ExecutionUnavailable):
        load_settings().validate_deployment(role="api")


def test_multi_node_requires_postgres_s3_and_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    with pytest.raises(ConfigError, match="refuses sqlite"):
        load_settings().validate_deployment(role="cli")


def _multi_node_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_DB_BACKEND", "postgres")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://recertia_app@db/recertia")
    monkeypatch.setenv("RECERTIA_BLOB_STORE", "s3")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("RECERTIA_API_KEY_PEPPER", "pepper")
    monkeypatch.setenv("RECERTIA_TENANT_SKILLS", "1")
    monkeypatch.setenv("RECERTIA_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("RECERTIA_TENANT_ENCRYPTION", "required")
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.delenv("RECERTIA_ALLOW_FAULTS", raising=False)
    monkeypatch.delenv("RECERTIA_CONSOLE_DEV_LOGIN", raising=False)


def test_multi_node_accepts_a_locked_down_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _multi_node_ready(monkeypatch, tmp_path)
    load_settings().validate_deployment(role="api")


def test_multi_node_refuses_faults_encryption_owner_and_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _multi_node_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("RECERTIA_ALLOW_FAULTS", "1")
    with pytest.raises(ConfigError, match="ALLOW_FAULTS"):
        load_settings().validate_deployment(role="api")
    monkeypatch.delenv("RECERTIA_ALLOW_FAULTS")
    monkeypatch.setenv("RECERTIA_TENANT_ENCRYPTION", "off")
    with pytest.raises(ConfigError, match="TENANT_ENCRYPTION"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_TENANT_ENCRYPTION", "required")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://recertia_owner@db/recertia")
    with pytest.raises(ConfigError, match="owner role"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://postgres@db/recertia")
    with pytest.raises(ConfigError, match="owner role"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://recertia_app@db/recertia")
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "off")
    with pytest.raises(ConfigError, match="CONSOLE_AUTH"):
        load_settings().validate_deployment(role="api")
    monkeypatch.setenv("RECERTIA_CONSOLE_AUTH", "oidc")
    monkeypatch.setenv("RECERTIA_CONSOLE_DEV_LOGIN", "1")
    with pytest.raises(ConfigError, match="CONSOLE_DEV_LOGIN"):
        load_settings().validate_deployment(role="api")


def test_multi_node_refuses_fast_durability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.setenv("RECERTIA_EXECUTION_BACKEND", "container")
    monkeypatch.setenv("RECERTIA_DB_BACKEND", "postgres")
    monkeypatch.setenv("RECERTIA_DATABASE_URL", "postgres://db")
    monkeypatch.setenv("RECERTIA_BLOB_STORE", "s3")
    monkeypatch.setenv("RECERTIA_CONSOLE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("RECERTIA_API_KEY_PEPPER", "pepper")
    monkeypatch.setenv("RECERTIA_DURABILITY", "fast")
    with pytest.raises(ConfigError, match="fast"):
        load_settings().validate_deployment(role="worker")
