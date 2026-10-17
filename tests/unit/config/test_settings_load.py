"""Settings load and deployment validation."""

from __future__ import annotations

import pytest

from recertia.errors import ConfigError, ExecutionUnavailable
from recertia.settings import load_settings


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
