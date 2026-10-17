# Agent notes

This file is for **coding agents** (Cursor Cloud and similar). It is **not** the contributor guide.

- Humans sending a change: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Security reports: [`SECURITY.md`](SECURITY.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)

## Cursor Cloud environment

Recertia is a single Python (>=3.11) package. The runtime is a cyclic graph orchestrator exposed
through two surfaces: the `recertia` CLI and a FastAPI HTTP API. Persistence is embedded SQLite +
files under a gitignored `.recertia/` dir; no external database is required for development.

### Environment / dependencies

- The startup update script runs `pip install -e ".[dev]"` (editable install with test + lint +
  API extras). Standard commands live in `pyproject.toml` and `.github/workflows/ci.yml`.
- Console scripts (`recertia`, `pytest`, `ruff`, `mypy`, `uvicorn`) install to `~/.local/bin`.
  That dir is added to `PATH` via `~/.bashrc`; if a fresh non-login shell can't find `recertia`,
  run `export PATH="$HOME/.local/bin:$PATH"`.

### Execution backend (important gotcha)

- Production default is `RECERTIA_EXECUTION_BACKEND=container` (Docker/Podman). **Docker is NOT
  installed in this environment**, so the container backend and the `container-smoke` CI job
  cannot run here.
- For development, always use the local executor instead:
  - CLI: add `--local-exec` to `recertia run` / `recertia resume`.
  - API: local backend is refused unless `RECERTIA_API_ALLOW_LOCAL_EXEC=1` is also set
    (break-glass). Without it, `POST /v1/runs` returns `503`.
  - API `script` fields require an API key with the `exec` scope (or `admin`).
- The pytest suite already forces the local backend + API break-glass via `tests/conftest.py`,
  so `pytest` needs no Docker.

### Lint / test / build

- Lint + types: `ruff check contracts/ src/ scripts/ tests/ conftest.py` and
  `mypy contracts/ src/recertia/`. Main CI fails closed on mypy (see #8).
- Default T2 policy is `policy/default.json` (`RECERTIA_POLICY_PATH` overrides). Do not
  write weekly `JobQuota` spend back into that file — spend is reserved/settled in
  `{runs_root}/jobs/job_quota.sqlite`. `{runs_root}/jobs/job_quota.json` is the operator
  export (CLI default `.recertia/jobs/job_quota.json`; the API writes
  `.recertia/runs/<tenant>/jobs/job_quota.json`).
- Console auth defaults **off** (`RECERTIA_CONSOLE_AUTH=off` ignores session cookies).
  Dev login needs `RECERTIA_CONSOLE_AUTH=dev` and `RECERTIA_CONSOLE_DEV_LOGIN=1`.
  OIDC requires `RECERTIA_CONSOLE_SESSION_SECRET`. Session cookie is `SameSite=Strict`
  with CSRF double-submit (`X-Recertia-CSRF`).
- Drift/hygiene checks (part of CI): the `--check` scripts under `scripts/`
  (`generate_schemas.py`, `export_examples.py`, `generate_architecture2.py`, `check_cross_refs.py`,
  `check_milestone_deps.py`, `check_assumptions_hygiene.py`, `security_review.py`). `schema/` is
  generated from `contracts/` — regenerate with `python3 scripts/generate_schemas.py` when
  contracts change, or CI's drift check fails. `docs/architecture2.md` is the all-in-one
  architecture + specifications compilation — regenerate with
  `python3 scripts/generate_architecture2.py`. Run a Python security review with
  `python3 scripts/security_review.py --check`.
- Tests: `pytest -v` (fast, ~10s).

### Running the surfaces

- CLI run (example): `recertia run --goal evals/golden/repo-chore/add-editorconfig/goal.json
  --local-exec --runs-root .recertia --workdir .recertia/ws`. Inspect with
  `recertia runs show <run_id> --route-log` and `recertia ledger verify`.
- HTTP API: `RECERTIA_EXECUTION_BACKEND=local uvicorn recertia.api.app:app --host 127.0.0.1 --port 8000`
  (default port 8000; interactive docs at `/docs` unless `RECERTIA_ENV=prod`).
  Endpoints under `/v1/*` require an `X-API-Key` header. Issue a key that writes to
  the same root the server uses (`.recertia/api_keys.sqlite`):
  `recertia keys issue --tenant demo --scopes runs,metrics --actor dev`.
  Default `POST /v1/runs` is sync; `mode=async` enqueues for `recertia worker`.
