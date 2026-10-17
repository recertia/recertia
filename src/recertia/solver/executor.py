"""Container and local command executors. One resolver for the backend."""

from __future__ import annotations

import os
from pathlib import Path

from contracts.policy import Policy
from recertia.errors import ExecutionUnavailable
from recertia.ports.execution import (
    CommandExecutor,
    CommandRequest,
    CommandResult,
    ExecutionBackend,
    ExecutionConfig,
    LocalExecutionCapability,
    SandboxLimits,
    Surface,
)
from recertia.settings import Settings
from recertia.solver.container import (
    _container_run,
    _local_run,
    backend_override,
    container_runtime,
)
from recertia.solver.sandbox import SandboxError


def resolve_backend(
    settings: Settings,
    policy: Policy | None,
    *,
    surface: Surface,
    capability: LocalExecutionCapability | None,
) -> ExecutionBackend:
    """The only reader of ``policy.isolation.default_backend``.

    Precedence: multi_node refusal, then an explicit capability on a surface
    that allows local, then a context override (``--local-exec``), then an
    operator-set backend, then the policy field, then ``container``.
    """

    def _refuse_local(reason: str) -> None:
        if settings.deployment_mode == "multi_node":
            raise ExecutionUnavailable(reason)

    if capability is not None:
        allowed = _surface_allows_local(settings, surface, capability)
        if allowed:
            _refuse_local("multi_node refuses the local execution backend")
            if settings.deployment_mode != "multi_node":
                return "local"
        # A capability that the surface does not honour does not select local.
    override = backend_override()
    if override is not None:
        if override == "local":
            _refuse_local("multi_node refuses the local execution backend")
        return override
    if settings.execution_backend_was_set:
        if settings.execution_backend == "local":
            _refuse_local("multi_node refuses the local execution backend")
        return settings.execution_backend
    if policy is not None:
        backend = policy.isolation.default_backend
        if backend == "local":
            _refuse_local("multi_node refuses the local execution backend")
        if backend in {"container", "local"}:
            return backend
    return "container"


def _surface_allows_local(
    settings: Settings, surface: Surface, capability: LocalExecutionCapability
) -> bool:
    if surface == "cli":
        return True
    if surface in {"api", "worker", "job"}:
        return settings.api_allow_local_exec
    if surface == "golden":
        return capability.purpose in {"cli", "api-break-glass", "pytest"}
    return False


def build_executor(
    *,
    surface: Surface,
    settings: Settings,
    capability: LocalExecutionCapability | None = None,
    policy: Policy | None = None,
) -> CommandExecutor:
    if surface == "golden" and settings.golden_backend == "container":
        return ContainerExecutor(settings.execution_config())
    if surface == "golden" and settings.golden_backend == "local":
        if capability is None:
            raise ExecutionUnavailable(
                "a local golden gate needs --local-exec or the API break-glass"
            )
        if settings.deployment_mode == "multi_node":
            raise ExecutionUnavailable("multi_node refuses the local execution backend")
        return LocalExecutor(settings.execution_config(), capability)
    backend = resolve_backend(settings, policy, surface=surface, capability=capability)
    config = settings.execution_config()
    if backend == "local":
        if capability is None:
            raise SandboxError("local execution requires an explicit LocalExecutionCapability")
        return LocalExecutor(config, capability)
    return ContainerExecutor(config)


class ContainerExecutor:
    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def probe(self) -> None:
        runtime = container_runtime() or self.config.runtime
        if runtime is None:
            raise SandboxError("no approved container runtime available (Docker or Podman required)")

    def run(self, request: CommandRequest) -> CommandResult:
        from recertia.solver.container import ContainerSpec, _enforce_container_policy

        runtime = container_runtime() or self.config.runtime
        if runtime is None:
            raise SandboxError("no approved container runtime available (Docker or Podman required)")
        spec = _enforce_container_policy(
            ContainerSpec(image=self.config.image, network="none", read_only_root=True)
        )
        if self.config.require_image_digest and "@sha256:" not in spec.image:
            raise SandboxError(
                f"container image {spec.image!r} must be digest-pinned (@sha256:)"
            )
        proc = _container_run(
            runtime,
            request.command,
            workdir=request.workdir,
            spec=spec,
            timeout_s=int(request.timeout_s),
            output_cap_bytes=request.output_cap_bytes or self.config.output_cap_bytes,
            run_id=request.run_id,
            owner_id=request.owner_id,
        )
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


class LocalExecutor:
    def __init__(self, config: ExecutionConfig, capability: LocalExecutionCapability) -> None:
        self.config = config
        self.capability = capability

    def run(self, request: CommandRequest) -> CommandResult:
        # SEC-5: model/skill shell must not reach the local backend even if a
        # caller bypasses shell_handler / enforce_model_shell.
        if request.origin in ("model", "skill"):
            raise SandboxError(
                "model-origin shell is refused unless the execution backend is container"
            )
        limits = request.limits or SandboxLimits(
            max_cpu_seconds=self.config.max_cpu_seconds,
            max_address_space_mb=self.config.max_address_space_mb,
            allowed_env_keys=self.config.allowed_env_keys,
        )
        home = str(request.workdir)
        if (
            self.config.local_home_passthrough
            and self.config.deployment_mode == "single_node"
        ):
            host_home = os.environ.get("HOME")
            if host_home:
                home = host_home
        proc = _local_run(
            request.command,
            workdir=request.workdir,
            limits=limits,
            timeout_s=int(request.timeout_s),
            home=home,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


def run_command(request: CommandRequest, *, executor: CommandExecutor) -> CommandResult:
    return executor.run(request)


def default_executor_for_tests(workdir: Path | None = None) -> LocalExecutor:
    del workdir
    from recertia.settings import load_settings

    settings = load_settings()
    return LocalExecutor(settings.execution_config(), LocalExecutionCapability(purpose="pytest"))
