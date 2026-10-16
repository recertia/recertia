"""Command execution port. ``ExecutionBackend`` is the enum; config is frozen."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

ExecutionBackend = Literal["container", "local"]
# ARCH-3 origins plus the SEC-5 names, so later slices do not add a second type.
CommandOrigin = Literal[
    "criterion",
    "script",
    "tool",
    "job",
    "probe",
    "caller",
    "model",
    "skill",
    "freeze",
]
Surface = Literal["cli", "api", "worker", "golden", "job"]

_DEFAULT_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TMPDIR")


@dataclass(frozen=True)
class SandboxLimits:
    """Resource and environment limits for one command. ``HOME`` is not inheritable."""

    max_cpu_seconds: int = 60
    max_address_space_mb: int = 512
    scrub_env: bool = True
    allowed_env_keys: tuple[str, ...] = _DEFAULT_ENV_KEYS
    allow_network: bool = False


@dataclass(frozen=True)
class ExecutionConfig:
    """Built by ``Settings.execution_config()``. Held by each executor."""

    backend: ExecutionBackend
    runtime: Literal["docker", "podman"] | None
    image: str
    allow_custom_image: bool
    workdir_world_write: bool
    api_allow_local_exec: bool
    local_home_passthrough: bool
    allowed_env_keys: tuple[str, ...] = _DEFAULT_ENV_KEYS
    max_cpu_seconds: int = 60
    max_address_space_mb: int = 512
    deployment_mode: Literal["single_node", "multi_node"] = "single_node"
    network: Literal["none"] = "none"
    user: str = "65534:65534"
    memory: str = "512m"
    memory_swap: str = "512m"
    cpus: str = "1"
    pids_limit: int = 256
    nofile_limit: int = 1024
    output_cap_bytes: int = 262144
    read_only_root: bool = True
    require_image_digest: bool = False


@dataclass(frozen=True)
class CommandRequest:
    command: str
    workdir: Path
    timeout_s: float = 60
    limits: SandboxLimits | None = None
    run_id: str | None = None
    owner_id: str | None = None
    fencing_token: str | None = None
    origin: CommandOrigin = "tool"
    output_cap_bytes: int | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    config: ExecutionConfig

    def run(self, request: CommandRequest) -> CommandResult: ...


@dataclass(frozen=True)
class LocalExecutionCapability:
    """Required to construct ``LocalExecutor``. Unchanged idea from the container module."""

    purpose: str = "test-or-local-development"
