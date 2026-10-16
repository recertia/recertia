"""Production command sandbox backed exclusively by Docker or Podman."""

from __future__ import annotations

import contextvars
import os
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from recertia.ports.execution import LocalExecutionCapability
from recertia.settings import load_settings
from recertia.solver.sandbox import SandboxError, SandboxLimits

Backend = Literal["container", "local"]

_ALLOWED_IMAGES = frozenset({"python:3.12-slim", "python:3.11-slim", "python:3.12", "python:3.11"})
_backend_override: contextvars.ContextVar[Backend | None] = contextvars.ContextVar(
    "recertia_execution_backend", default=None
)


@dataclass(frozen=True)
class ContainerSpec:
    """Isolation contract requested of a container backend."""

    image: str = "python:3.12-slim"
    network: str = "none"
    read_only_root: bool = True
    user: str = "65534:65534"  # nobody
    workdir_mount: str = "/work"
    remove: bool = True


def container_runtime() -> str | None:
    """Return the explicitly requested or first approved OCI runtime."""

    requested = load_settings().container_runtime
    if requested:
        if requested not in {"docker", "podman"}:
            return None
        return requested if shutil.which(requested) else None
    return next((runtime for runtime in ("docker", "podman") if shutil.which(runtime)), None)


def backend_override() -> Backend | None:
    """Context-local backend from ``--local-exec``. ``None`` when unset."""

    return _backend_override.get()


def configured_backend() -> Backend:
    """Read the execution mode for this context; production defaults to OCI.

    A context override (CLI ``--local-exec``, tests) wins over the process
    environment. The process environment is never written by this module.
    """

    override = _backend_override.get()
    backend = override if override is not None else load_settings().execution_backend
    if backend not in {"container", "local"}:
        raise SandboxError(f"unsupported execution backend: {backend!r}")
    return backend  # type: ignore[return-value]


@contextmanager
def execution_backend_override(backend: Backend) -> Iterator[None]:
    """Select a backend for the current context without mutating ``os.environ``."""

    if backend not in {"container", "local"}:
        raise SandboxError(f"unsupported execution backend: {backend!r}")
    token = _backend_override.set(backend)
    try:
        yield
    finally:
        _backend_override.reset(token)


def allow_custom_image() -> bool:
    """True only for an explicit opt-in, not for any non-empty string.

    Accepted values (case-insensitive): ``1``, ``true``, ``yes``, ``on``.
    ``0``, ``false``, and any other value stay on the image allowlist.
    """

    return load_settings().allow_custom_image


def local_execution_capability() -> LocalExecutionCapability | None:
    """Grant local execution only after an explicit configuration opt-in."""

    return LocalExecutionCapability() if configured_backend() == "local" else None


def ensure_execution_ready() -> Backend:
    """Fail fast with guidance when the configured backend cannot run commands.

    Avoids burning solve attempts on a missing Docker/Podman install. Callers that
    want the non-OCI path must set ``RECERTIA_EXECUTION_BACKEND=local`` (CLI:
    ``--local-exec``).
    """

    backend = configured_backend()
    if backend == "container" and container_runtime() is None:
        raise SandboxError(
            "RECERTIA_EXECUTION_BACKEND=container but no Docker/Podman is available. "
            "Install Docker or Podman, or set RECERTIA_EXECUTION_BACKEND=local "
            "(CLI: recertia run --local-exec) for development."
        )
    if backend == "local" and local_execution_capability() is None:
        raise SandboxError("local execution capability missing despite RECERTIA_EXECUTION_BACKEND=local")
    return backend


def ensure_api_execution_ready() -> Backend:
    """Like :func:`ensure_execution_ready`, but refuse ``local`` without break-glass.

    Host-local execution via the HTTP API is a direct RCE surface for any
    ``runs``-scoped key. Require ``RECERTIA_API_ALLOW_LOCAL_EXEC=1`` explicitly.
    """

    backend = ensure_execution_ready()
    if backend == "local":
        if not load_settings().api_allow_local_exec:
            raise SandboxError(
                "RECERTIA_EXECUTION_BACKEND=local is not allowed for the HTTP API. "
                "Use the container backend, or set RECERTIA_API_ALLOW_LOCAL_EXEC=1 "
                "as an explicit break-glass for development only."
            )
    return backend


def ensure_workdir_writable_by_container(workdir: Path) -> None:
    """Ensure the bind-mounted workdir is writable by the sandbox user (nobody).

    Containers run as ``65534:65534``. Host-created directories are often ``0755`` owned
    by the invoking user, which blocks writes inside the sandbox. Default mode adds
    owner/group write (``0770``). World-write (``0777``) is opt-in via
    ``RECERTIA_WORKDIR_WORLD_WRITE=1`` for hosts without a shared GID / rootless map.

    On Windows, POSIX mode bits are not enforced on NTFS; this is a no-op after the
    directory-existence check (Linux bind-mount hosts are the intended audience).
    """

    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxError(f"workdir does not exist: {workdir}")
    if sys.platform == "win32":
        return
    mode = workdir.stat().st_mode
    new_mode = mode | 0o0770
    if load_settings().workdir_world_write:
        new_mode |= 0o0007
    workdir.chmod(new_mode)


def default_container_image() -> str:
    """Allowlisted image tag; optional ``RECERTIA_CONTAINER_IMAGE`` override."""

    image = load_settings().container_image
    # Accept ``tag`` or ``tag@sha256:…``; allowlist checks the tag portion.
    tag = image.split("@", 1)[0]
    if tag not in _ALLOWED_IMAGES and not allow_custom_image():
        raise SandboxError(
            f"container image {image!r} is not on the allowlist {_ALLOWED_IMAGES}"
        )
    return image


def probe_container_runtime(*, timeout_s: int = 60) -> None:
    """Run a trivial container command; raise ``SandboxError`` if the runtime is broken."""

    runtime = container_runtime()
    if runtime is None:
        raise SandboxError("no approved container runtime available (Docker or Podman required)")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="recertia-probe-") as tmp:
        workdir = Path(tmp)
        ensure_workdir_writable_by_container(workdir)
        proc = run_in_container("true", workdir=workdir, timeout_s=timeout_s)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise SandboxError(
                f"container probe failed via {runtime}: exit={proc.returncode} {detail[:500]}"
            )



def _image_tag(image: str) -> str:
    return image.split("@", 1)[0]


def run_in_container(
    command: str,
    *,
    workdir: Path,
    limits: SandboxLimits | None = None,
    spec: ContainerSpec | None = None,
    timeout_s: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` inside a container with no network and a bound workdir.

    There is deliberately no local-process or simulated fallback.  A command
    that cannot be run in an approved OCI runtime is rejected before execution.
    """

    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxError(f"workdir does not exist: {workdir}")
    ensure_workdir_writable_by_container(workdir)
    limits = limits or SandboxLimits(allow_network=False)
    if spec is None:
        spec = ContainerSpec(image=default_container_image())
    spec = _enforce_container_policy(spec)
    if limits.allow_network:
        raise SandboxError("container backend refuses allow_network=True")

    runtime = container_runtime()
    if runtime is None:
        raise SandboxError("no approved container runtime available (Docker or Podman required)")
    return _container_run(runtime, command, workdir=workdir, spec=spec, timeout_s=timeout_s)


def _enforce_container_policy(spec: ContainerSpec) -> ContainerSpec:
    """Normalize caller-supplied specs to the immutable sandbox policy."""

    if spec.network != "none":
        raise SandboxError(f"container network {spec.network!r} is not allowed")
    if spec.user in {"0", "0:0", "root", "root:root"}:
        raise SandboxError("container root user is not allowed")
    if not spec.read_only_root:
        raise SandboxError("writable container root filesystem is not allowed")
    if not spec.workdir_mount.startswith("/"):
        raise SandboxError("workdir_mount must be an absolute container path")
    tag = _image_tag(spec.image)
    if tag not in _ALLOWED_IMAGES and not allow_custom_image():
        raise SandboxError(f"container image {spec.image!r} is not on the allowlist")
    return spec


def _container_run(
    runtime: str,
    command: str,
    *,
    workdir: Path,
    spec: ContainerSpec,
    timeout_s: int,
    output_cap_bytes: int | None = None,
    run_id: str | None = None,
    owner_id: str | None = None,
    tenant_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    name = f"rec-{uuid.uuid4().hex[:16]}"
    memory = "512m"
    args = [
        runtime,
        "run",
        "--rm" if spec.remove else "",
        f"--name={name}",
        f"--network={spec.network}",
        "--ipc",
        "none",
        f"--user={spec.user}",
        f"--workdir={spec.workdir_mount}",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--ulimit",
        "nproc=256",
        "--ulimit",
        "nofile=1024",
        "--mount",
        f"type=bind,source={workdir},destination={spec.workdir_mount}",
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--cpus",
        "1",
        "-e",
        f"HOME={spec.workdir_mount}",
    ]
    if tenant_id:
        args.extend(["--label", f"recertia.tenant={tenant_id}"])
    if run_id:
        args.extend(["--label", f"recertia.run_id={run_id}"])
    if owner_id:
        args.extend(["--label", f"recertia.owner_id={owner_id}"])
    # Digest-pinned images should not be retargeted by a mutable tag pull.
    if "@sha256:" in spec.image:
        args.extend(["--pull", "never"])
    if spec.read_only_root:
        args.append("--read-only")
        args.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m"])
    args = [a for a in args if a]
    args.extend([spec.image, "sh", "-c", command])
    cap = output_cap_bytes if output_cap_bytes is not None else 0
    try:
        if cap > 0:
            return _run_capped(args, runtime=runtime, name=name, timeout_s=timeout_s, cap=cap)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # Killing the CLI client does not stop the container; kill it by name.
        _kill_container(runtime, name)
        raise


def _run_capped(
    args: list[str],
    *,
    runtime: str,
    name: str,
    timeout_s: int,
    cap: int,
) -> subprocess.CompletedProcess[str]:
    """Stream stdout and stderr, killing the container once the byte cap is hit."""

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    total = 0
    try:
        assert proc.stdout is not None and proc.stderr is not None
        # Read stderr on a side thread so a full stderr pipe cannot stall stdout.
        import threading

        stderr_box: list[str] = []

        def _read_err() -> None:
            assert proc.stderr is not None
            stderr_box.append(proc.stderr.read())

        thread = threading.Thread(target=_read_err, daemon=True)
        thread.start()
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            stdout_parts.append(chunk)
            total += len(chunk.encode("utf-8", errors="replace"))
            if total > cap:
                _kill_container(runtime, name)
                proc.kill()
                thread.join(timeout=2)
                return subprocess.CompletedProcess(
                    args,
                    125,
                    "".join(stdout_parts),
                    ("".join(stderr_box) + "\noutput cap exceeded").strip(),
                )
        proc.wait(timeout=timeout_s)
        thread.join(timeout=2)
        stderr_parts = stderr_box
    except subprocess.TimeoutExpired:
        _kill_container(runtime, name)
        proc.kill()
        raise
    return subprocess.CompletedProcess(
        args, proc.returncode or 0, "".join(stdout_parts), "".join(stderr_parts)
    )


def _kill_container(runtime: str, name: str) -> None:
    """Best-effort ``<runtime> kill <name>`` after a timeout (never raises)."""

    try:
        subprocess.run([runtime, "kill", name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the child's whole process group (POSIX); plain kill on Windows."""

    if sys.platform == "win32":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_with_backend(
    command: str,
    *,
    workdir: Path,
    backend: Backend = "container",
    limits: SandboxLimits | None = None,
    timeout_s: int = 60,
    image: str | None = None,
    local_capability: LocalExecutionCapability | None = None,
) -> subprocess.CompletedProcess[str]:
    if backend == "local":
        if local_capability is None:
            raise SandboxError("local execution requires an explicit LocalExecutionCapability")
        return _local_run(command, workdir=workdir, limits=limits or SandboxLimits(), timeout_s=timeout_s)
    if backend != "container":
        raise SandboxError(f"unsupported execution backend: {backend!r}")
    spec = ContainerSpec(image=image or default_container_image())
    return run_in_container(
        command,
        workdir=workdir,
        limits=limits,
        spec=spec,
        timeout_s=timeout_s,
    )


def run_configured_command(
    command: str,
    *,
    workdir: Path,
    limits: SandboxLimits | None = None,
    timeout_s: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run via OCI by default, or the explicitly opted-in local capability."""

    backend = configured_backend()
    return run_with_backend(
        command,
        workdir=workdir,
        backend=backend,
        limits=limits,
        timeout_s=timeout_s,
        local_capability=local_execution_capability(),
    )


def _local_run(
    command: str,
    *,
    workdir: Path,
    limits: SandboxLimits,
    timeout_s: int,
    home: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Bounded local executor for explicitly opted-in test/development use only."""

    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxError(f"workdir does not exist: {workdir}")
    # Never inherit a host HOME. The child home is the workdir even when a
    # caller-supplied allowlist still names HOME.
    env = {
        key: value
        for key, value in os.environ.items()
        if key in limits.allowed_env_keys and key != "HOME"
    }
    env["HOME"] = home or str(workdir)
    if sys.platform == "win32":
        env.setdefault("PATH", os.environ.get("PATH", ""))
        env.setdefault("TMP", str(workdir))
        env.setdefault("TEMP", str(workdir))
    else:
        env.setdefault("PATH", "/usr/bin:/bin")
        env.setdefault("TMPDIR", str(workdir))

    kwargs: dict = {
        "shell": True,  # recertia-security-ok: local executor; gated by LocalExecutionCapability
        "cwd": workdir,
        "capture_output": True,
        "text": True,
        "timeout": timeout_s,
        "env": env,
    }
    # preexec_fn + resource.setrlimit are Unix-only (not available on Windows).
    if sys.platform != "win32":
        import resource

        def limit_process() -> None:
            # The platform has setrlimit (non-Windows). A failure must not run
            # the command unlimited — fail closed in the child before exec.
            try:
                resource.setrlimit(
                    resource.RLIMIT_CPU, (limits.max_cpu_seconds, limits.max_cpu_seconds)
                )
                bytes_cap = limits.max_address_space_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (bytes_cap, bytes_cap))
            except (OSError, ValueError) as exc:
                raise SandboxError(f"setrlimit failed: {exc}") from exc

        kwargs["preexec_fn"] = limit_process

    # Own session/process group: on timeout kill the whole tree, not just /bin/sh.
    del kwargs["timeout"], kwargs["capture_output"]
    try:
        # preexec_fn exceptions are not reconstructed as SandboxError (vfork
        # reports a builtin SubprocessError). A failed setrlimit must still
        # surface as a closed sandbox, not as a started command.
        proc_cm = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **kwargs,
        )
    except subprocess.SubprocessError as exc:
        raise SandboxError(f"setrlimit failed: {exc}") from exc
    with proc_cm as proc:
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(command, proc.returncode, out, err)
