# Container sandbox setup

Recertia's production execution backend runs solve/validate commands inside an OCI
container (Docker or Podman). This document covers setup, permissions, hardening,
and CI.

## Quick start

```bash
# 1. Install Docker Engine or Podman and confirm the CLI works
docker version   # or: podman version

# 2. Pull an allowlisted image
docker pull python:3.12-slim

# 3. Use the container backend (this is the default)
export RECERTIA_EXECUTION_BACKEND=container
# optional pin:
# export RECERTIA_CONTAINER_RUNTIME=docker

# 4. Smoke-test
python3 scripts/smoke_container.py
```

Development escape hatch (no OCI): `recertia run --local-exec …` or
`RECERTIA_EXECUTION_BACKEND=local`. `--local-exec` does not write process env.
`RECERTIA_DEPLOYMENT_MODE=multi_node` refuses the local backend
([ADR-0021](../adr/0021-deployment-model.md)). The local executor drops provider
and tenant secrets from the child environment.

## Runtime selection

| Variable | Purpose |
| --- | --- |
| `RECERTIA_EXECUTION_BACKEND` | `container` (default) or `local` |
| `RECERTIA_CONTAINER_RUNTIME` | Force `docker` or `podman` when both are installed |
| `RECERTIA_CONTAINER_IMAGE` | Allowlisted tag, optionally pinned: `python:3.12-slim@sha256:…` |
| `RECERTIA_ALLOW_CUSTOM_IMAGE` | Set to allow a non-allowlisted image (reviewed exceptions only) |
| `RECERTIA_API_ALLOW_LOCAL_EXEC` | Break-glass: allow `local` backend on the HTTP API (dev only) |
| `RECERTIA_WORKDIR_WORLD_WRITE` | Opt-in `0777` on bind-mounted workdirs (prefer shared GID / rootless) |

Allowlisted tags: `python:3.12-slim`, `python:3.11-slim`, `python:3.12`, `python:3.11`.

## Sandbox policy (immutable)

Every container invocation uses:

- `--network=none`
- read-only root filesystem + tmpfs `/tmp`
- user `65534:65534` (nobody)
- workdir bind-mounted at `/work:rw`
- `--cap-drop=ALL`, `no-new-privileges`
- memory / CPU caps

There is no silent host-process fallback when the backend is `container`.

## Permissions and bind mounts

The sandbox user is **nobody** (`65534`). Host workdirs created as `0755` owned by
your login often block writes inside the container.

Recertia calls `ensure_workdir_writable_by_container` before each invocation (adds
other-write/traverse on the workdir). You should still:

1. Keep runs under a path the runtime may bind-mount (Docker Desktop: grant file sharing).
2. On Linux, ensure your user can talk to the daemon (`usermod -aG docker $USER` then
   re-login, or rootless Podman). A `permission denied … docker.sock` error means the CLI
   is present but the daemon socket is not writable to your user.
3. For rootless Podman, confirm UID mapping so `65534` can write the mounted volume.
4. Avoid placing `.recertia/workspaces` on filesystems that reject `chmod` or overlay mounts.

If smoke fails with permission errors on `/work/...`, check workdir mode (`ls -ld`) and
daemon file-sharing settings before changing sandbox policy.

## Hardening

- **Pin digests in production.** Pull and reference a digest while keeping an allowlisted tag:
  ```bash
  docker pull python:3.12-slim@sha256:<digest>
  export RECERTIA_CONTAINER_IMAGE=python:3.12-slim@sha256:<digest>
  ```
- **Pre-pull in CI** so jobs do not race the registry (see `.github/workflows/ci.yml`
  `container-smoke` job).
- **Do not set `RECERTIA_ALLOW_CUSTOM_IMAGE`** outside a reviewed change.
- Prefer Docker Engine or Podman on the host; do not weaken `--network=none` or run as root.

## Smoke test

```bash
python3 scripts/smoke_container.py
# or via pytest (skipped automatically when no working runtime):
pytest -v tests/e2e/test_container_smoke.py
```

Success criteria: exit 0, `terminal=solved`, and `SMOKE_OK` written in the workdir via the
container path (not `--local-exec`).
