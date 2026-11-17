"""Invariants for the product API/worker image (no Docker required)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
COMPOSE = REPO / "docker-compose.app.yml"
SANDBOX = REPO / "docker-compose.app.sandbox.yml"
ENTRYPOINT = REPO / "docker" / "entrypoint.sh"

_FROM = re.compile(
    r"^FROM python:3\.12-slim@sha256:([0-9a-f]{64})\s*$",
    re.M,
)


def test_dockerfile_pins_python_312_slim_index_digest() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = _FROM.search(text)
    assert match, "Dockerfile FROM must be python:3.12-slim@sha256:<64 hex>"
    assert ":latest" not in text
    assert "RECERTIA_ALLOW_CUSTOM_IMAGE" not in text
    assert "useradd --uid 65532" in text
    assert "--no-proxy-headers" in text
    assert '".[api,postgres]"' in text


def _service_env(doc: dict, name: str) -> dict[str, str]:
    raw = (doc.get("services") or {}).get(name, {}).get("environment") or {}
    assert isinstance(raw, dict), raw
    return {str(k): str(v) for k, v in raw.items()}


def test_compose_sandbox_image_matches_dockerfile_base() -> None:
    from_line = _FROM.search(DOCKERFILE.read_text(encoding="utf-8"))
    assert from_line is not None
    digest = "sha256:" + from_line.group(1)
    compose = COMPOSE.read_text(encoding="utf-8")
    assert f"python:3.12-slim@{digest}" in compose
    doc = yaml.safe_load(compose)
    for name in ("api", "worker"):
        env = _service_env(doc, name)
        assert env["RECERTIA_EXECUTION_BACKEND"] == "container"
        assert "RECERTIA_ALLOW_CUSTOM_IMAGE" not in env
        assert "RECERTIA_API_ALLOW_LOCAL_EXEC" not in env
        assert env["RECERTIA_CONTAINER_IMAGE"] == f"python:3.12-slim@{digest}"
        volumes = [str(v) for v in doc["services"][name].get("volumes") or []]
        assert not any("docker.sock" in v for v in volumes)
    assert "--no-proxy-headers" in compose
    assert "recertia_soak_pg" in compose
    assert "pg_isready -U recertia -d recertia" in compose
    assert "pgvector/pgvector:pg16@sha256:" in compose


def test_sandbox_overlay_mounts_socket_on_worker_only() -> None:
    doc = yaml.safe_load(SANDBOX.read_text(encoding="utf-8"))
    services = doc["services"]
    api_vols = [str(v) for v in services["api"].get("volumes") or []]
    worker_vols = [str(v) for v in services["worker"].get("volumes") or []]
    sock = "/var/run/docker.sock:/var/run/docker.sock"
    assert sock not in api_vols
    assert sock in worker_vols
    for name in ("api", "worker"):
        env = _service_env(doc, name)
        assert "RECERTIA_API_ALLOW_LOCAL_EXEC" not in env
        assert "RECERTIA_ALLOW_CUSTOM_IMAGE" not in env
    assert "privileged:" not in SANDBOX.read_text(encoding="utf-8")
    worker_cmd = [str(p) for p in services["worker"].get("command") or []]
    assert "uvicorn" not in worker_cmd


def test_dockerignore_keeps_runtime_state_and_heldout_out_of_the_image() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for token in (".recertia", ".venv", ".git", "evals", ".env", "heldout", "tests"):
        assert token in text, token
    assert (REPO / "docker" / "app.env.example").is_file()
    entry = ENTRYPOINT.read_text(encoding="utf-8")
    assert "setpriv" in entry and "65532" in entry


def test_no_committed_compose_secret_values() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    example = (REPO / "docker" / "app.env.example").read_text(encoding="utf-8")
    for text in (compose, example, SANDBOX.read_text(encoding="utf-8")):
        assert "sk-" not in text
        assert "sk-ant-" not in text
        assert not re.search(r"OPENAI_API_KEY:\s*['\"]?[A-Za-z0-9]", text)
        assert not re.search(r"POSTGRES_PASSWORD:\s*['\"]?[A-Za-z0-9]", text)
    for line in example.splitlines():
        if line.startswith("OPENAI_API_KEY=") or line.startswith("POSTGRES_PASSWORD="):
            assert line.endswith("="), line
