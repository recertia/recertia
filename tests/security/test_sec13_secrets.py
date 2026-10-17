"""Per-tenant provider keys stay out of other tenants and out of the executor."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from recertia.config import ModelConfig, resolve_api_key
from recertia.errors import ConfigError
from recertia.ports.execution import SandboxLimits
from recertia.secrets.model_keys import scrub_executor_env
from recertia.solver.container import _local_run


def _cfg() -> ModelConfig:
    return ModelConfig(provider="openai", model_id="gpt-test", api_key_env="OPENAI_API_KEY")


def test_broker_uses_only_the_callers_tenant_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "RECERTIA_TENANT_MODEL_KEYS",
        json.dumps(
            {
                "acme": {"openai": "acme-openai-key"},
                "beta": {"openai": "beta-openai-key"},
            }
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "shared-process-key")
    assert resolve_api_key(_cfg(), tenant="acme") == "acme-openai-key"
    assert resolve_api_key(_cfg(), tenant="beta") == "beta-openai-key"
    assert resolve_api_key(_cfg(), tenant="other") is None


def test_file_map_is_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"acme": {"anthropic": "acme-anthropic"}}), encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setenv("RECERTIA_TENANT_MODEL_KEYS_FILE", str(path))
    cfg = ModelConfig(provider="anthropic", model_id="claude", api_key_env="ANTHROPIC_API_KEY")
    with pytest.raises(ConfigError, match="0600"):
        resolve_api_key(cfg, tenant="acme")
    path.chmod(0o600)
    assert resolve_api_key(cfg, tenant="acme") == "acme-anthropic"
    assert resolve_api_key(cfg, tenant="beta") is None


def test_multi_node_does_not_read_the_process_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECERTIA_DEPLOYMENT_MODE", "multi_node")
    monkeypatch.delenv("RECERTIA_TENANT_MODEL_KEYS", raising=False)
    monkeypatch.delenv("RECERTIA_TENANT_MODEL_KEYS_FILE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "shared-process-key")
    assert resolve_api_key(_cfg(), tenant="acme") is None
    assert resolve_api_key(_cfg()) == "shared-process-key"


def test_executor_env_has_no_provider_key_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "tenant-a-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "tenant-b-openai")
    monkeypatch.setenv(
        "RECERTIA_TENANT_MODEL_KEYS",
        json.dumps({"acme": {"openai": "acme-openai-key"}, "beta": {"openai": "beta-openai-key"}}),
    )
    allowed = ("PATH", "LANG", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RECERTIA_TENANT_MODEL_KEYS")
    scrubbed = scrub_executor_env(os.environ, allowed)
    blob = "\n".join(scrubbed.values())
    assert "ANTHROPIC_API_KEY" not in scrubbed
    assert "OPENAI_API_KEY" not in scrubbed
    assert "RECERTIA_TENANT_MODEL_KEYS" not in scrubbed
    assert "tenant-a-anthropic" not in blob
    assert "tenant-b-openai" not in blob
    assert "acme-openai-key" not in blob
    assert "beta-openai-key" not in blob

    proc = _local_run(
        "python3 -c 'import os; print(repr(sorted(os.environ)))'",
        workdir=tmp_path,
        limits=SandboxLimits(allowed_env_keys=allowed),
        timeout_s=30,
    )
    assert proc.returncode == 0
    child = proc.stdout
    assert "ANTHROPIC_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "RECERTIA_TENANT_MODEL_KEYS" not in child
    assert "tenant-a-anthropic" not in child
    assert "acme-openai-key" not in child
    assert "beta-openai-key" not in child
