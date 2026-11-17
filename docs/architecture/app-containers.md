# Packaged API and worker containers

Digest-pinned image for Recertia’s HTTP API (product console at `/console`) and
the `recertia worker` process. This is **not** the execution sandbox image
(stock `python:3.12-slim`, allowlisted in `recertia.solver.container`) and not
the `skills/pin-dockerfile-base` practice skill.

Companion: [container-sandbox.md](container-sandbox.md), [go-live.md](go-live.md).

## What is in the image

| Item | Choice |
| --- | --- |
| Base | `python:3.12-slim` index digest (Python 3.12.16, debian trixie slim) |
| User | uid/gid `65532` (`recertia`), distinct from sandbox nobody `65534` |
| Deps | `pip install ".[api,postgres]"` from `pyproject.toml` |
| Lockfile | **None in this repo** (no `uv.lock` / `poetry.lock` / `requirements.txt`). Direct floors are the pin; transitives resolve from PyPI at build time |
| Secrets | None. Compose passes them via host env / `.env` |
| Sandbox CLI | **Not** installed. The image is the control plane, not an OCI runtime |

Pinned base (resolved 2026-11-17 from `registry-1.docker.io`,
`Docker-Content-Digest` on `manifests/3.12-slim`):

```text
python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
```

The held-out operator workflow pins an older index digest of the same tag for
the **sandbox** image. That pin is independent; do not retarget
`RECERTIA_CONTAINER_IMAGE` at the Recertia app image.

## Build

From the repository root (BuildKit not required):

```bash
docker build -t recertia:local .
```

Do not `docker push`. This tree does not define a registry.

Confirm the base:

```bash
docker image inspect recertia:local --format '{{.Config.User}} {{json .Config.Cmd}}'
docker run --rm --entrypoint python recertia:local -c 'import recertia,sys; print(sys.version)'
```

## Run (compose)

```bash
cp docker/app.env.example .env   # gitignored; fill POSTGRES_PASSWORD and any model keys
docker compose -f docker-compose.app.yml up --build
# console: http://127.0.0.1:8080/console
# liveness: http://127.0.0.1:8080/health
```

Uvicorn inside the container listens on `0.0.0.0:8080` so the port mapping
works, and **must** keep `--no-proxy-headers` (see [go-live.md](go-live.md)).
Compose publishes `127.0.0.1:8080:8080` and `127.0.0.1:5432:5432`.

| Service | Process | Data |
| --- | --- | --- |
| `api` | `python -m uvicorn recertia.api.app:app --host 0.0.0.0 --port 8080 --no-proxy-headers` | bind `./.recertia` |
| `worker` | `recertia worker --root /home/recertia/.recertia --concurrency 1` | same bind |
| `postgres` | `pgvector/pgvector:pg16` digest-pinned; `pg_isready` healthcheck | volume `recertia_soak_pg` (same name as `docker-compose.soak.yml`) |

`DATABASE_URL` is optional. Empty keeps sqlite under `.recertia/` (`single_node`).
Set it to `postgresql://recertia:<password>@postgres:5432/recertia` to use the
compose Postgres. Migrations: `docker compose -f docker-compose.app.yml run --rm api python3 /opt/recertia/scripts/soak_postgres.py --recertia-root /home/recertia/.recertia`.

To reuse an already-initialized soak volume, `POSTGRES_PASSWORD` must match
whatever created it. `docker-compose.soak.yml` currently commits a local
placeholder password; do not copy that value into git as an app default.

Console auth stays **off** unless you set `RECERTIA_CONSOLE_AUTH` in `.env`.
Dev login still needs `RECERTIA_CONSOLE_AUTH=dev` and `RECERTIA_CONSOLE_DEV_LOGIN=1`.

## Worker / sandbox design

The production execution backend is `container` (`RECERTIA_EXECUTION_BACKEND`,
default). From **inside** a worker container that is not an extra choice:

| Approach | What it does | Verdict here |
| --- | --- | --- |
| Privileged Docker-in-Docker | Worker daemon in the container, usually `--privileged` | Rejected. Weakens isolation of everything on the node |
| Docker socket + CLI (sibling containers) | Worker talks to the **host** daemon; sandboxes are siblings | Opt-in overlay only. Socket access is host-root equivalent for the worker |
| `RECERTIA_EXECUTION_BACKEND=local` in compose | Commands run as the worker uid in the app image | Rejected as a default. Silent sandbox downgrade |
| No socket, backend stays `container` | Worker starts; solves fail closed when no OCI runtime is visible | **Default compose** |

Default `docker-compose.app.yml`:

- Does **not** mount `/var/run/docker.sock`
- Does **not** set `RECERTIA_ALLOW_CUSTOM_IMAGE`
- Does **not** set `RECERTIA_API_ALLOW_LOCAL_EXEC`
- Sets `RECERTIA_CONTAINER_IMAGE` to the stock allowlisted `python:3.12-slim@sha256:…` (never the app image)

`POST /v1/runs` and a claimed hop then fail with the existing
`SandboxError` (“no Docker/Podman”) instead of running host-local commands.
The console, `/health`, and an idle worker still come up.

**Recommended** for real solves: run `recertia worker --root .recertia` on the
host (or a node that already has Docker/Podman), same as
[container-sandbox.md](container-sandbox.md). Compose is the control plane.

**Opt-in sibling overlay** (Linux + Docker Engine):

```bash
RECERTIA_REPO="$(pwd)" docker compose -f docker-compose.app.yml -f docker-compose.app.sandbox.yml up --build
```

The overlay bind-mounts the checkout at the **same absolute path** inside
`api`/`worker` (so workdir bind mounts are host paths) and mounts
`/var/run/docker.sock` **only on `worker`**. `api` gets a read-only docker CLI
bind so `ensure_api_execution_ready` can see a runtime without granting it a
daemon socket. Spawned sandboxes still get `--network=none`, user `65534:65534`,
read-only root, `--cap-drop=ALL`, `no-new-privileges`. Docker Desktop path
virtualization can break same-path binds; treat the overlay as Linux Engine.

## Refresh the base digest

The Dockerfile pins the **index** digest of tag `3.12-slim` (all architectures),
not a single-arch manifest.

```bash
TOKEN=$(curl -fsSL 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -fsSI \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json' \
  https://registry-1.docker.io/v2/library/python/manifests/3.12-slim \
  | grep -i docker-content-digest
```

Expect `org.opencontainers.image.version` similar to `3.12.x-slim-trixie` on the
linux/amd64 child. Put the index `sha256:…` in:

1. `Dockerfile` `FROM python:3.12-slim@sha256:…`
2. `docker-compose.app.yml` `RECERTIA_CONTAINER_IMAGE` (sandbox pin)
3. the table in this document

Rebuild with `docker build -t recertia:local .` and do not push.

Postgres in compose is pinned the same way against `pgvector/pgvector:pg16`
(`docker-compose.soak.yml` still uses the floating tag). Refresh:

```bash
TOKEN=$(curl -fsSL 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:pgvector/pgvector:pull' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -fsSI \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json' \
  https://registry-1.docker.io/v2/pgvector/pgvector/manifests/pg16 \
  | grep -i docker-content-digest
```

## CI

`.github/workflows/ci.yml` job `image-build` runs `docker build` (no push) when
a PR or push range touches the Docker files. It does not run the suite, does
not start compose, and does not change REL-3 probe skip/skipif guards.
