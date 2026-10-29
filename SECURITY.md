# Security policy

## Supported versions

This repository is pre-1.0 (`0.4.x` on `main`). Fixes land on `main`; there are no
long-lived release branches yet. Older release lines (0.1.x, 0.2.x and 0.3.x) get no
fixes; upgrade to 0.4.x to receive them.

## Reporting a vulnerability

**Do not** open a public GitHub issue or pull request for a security report.

Use GitHub's private vulnerability reporting on this repository (the **Security**
tab → **Report a vulnerability**). Include:

- Affected surface (CLI, HTTP API, console, container executor, skill promotion, …)
- Recertia version or `main` commit SHA
- Steps to reproduce, or a minimal fixture
- Impact (what an attacker with what access can do)

Please allow a few days for a first response. If the report is accepted, we will
coordinate a fix on `main` before any public write-up.

## Out of scope (file as a normal issue)

- Operator misconfiguration (`RECERTIA_COMMAND_POLICY=off`,
  `RECERTIA_API_ALLOW_LOCAL_EXEC=1`, `--local-exec` on a shared host)
- Secrets the operator stored under `.recertia/` or in the environment
- Model-provider account compromise outside Recertia

## Hardening notes

Production solves should use `RECERTIA_EXECUTION_BACKEND=container`. The local
executor and API break-glass flags are development-only. See
[`docs/architecture/container-sandbox.md`](docs/architecture/container-sandbox.md)
and [`docs/architecture/go-live.md`](docs/architecture/go-live.md).
