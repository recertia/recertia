# Contributing

Recertia is a single Python (>=3.11) package. Contracts in [`contracts/`](contracts)
are the structural source of truth (ADR-0009). Implementation lives in
[`src/recertia/`](src/recertia).

Contributions are accepted under the [PolyForm Noncommercial License 1.0.0](LICENSE).
This is **not** MIT. Do not send a change that assumes commercial-use rights.

## Canonical GitHub identity

This project lives at **[github.com/recertia/recertia](https://github.com/recertia/recertia)**
(org `recertia`, repo `recertia`). Clone and push only that URL. A pull request
shows the GitHub owner/repo of the remote you pushed to; you cannot retarget an
existing PR to a different owner or name.

```bash
git clone https://github.com/recertia/recertia.git
git remote set-url origin https://github.com/recertia/recertia.git
git remote -v   # origin must be github.com/recertia/recertia
```

Cursor Cloud Agents open PRs against the GitHub repository selected when the
agent starts — start them from `recertia/recertia`, not a differently named
clone or an old org. Historical PRs from a previous owner stay as they are.

## Setup

```bash
pip install -e ".[dev]"
export PATH="$HOME/.local/bin:$PATH"   # if `recertia` is not on PATH
```

Docker is required only for the container executor. Local development and the
pytest suite use the local backend:

```bash
recertia run --goal evals/golden/repo-chore/add-editorconfig/goal.json --local-exec
pytest -v
```

API `POST /v1/runs` refuses the local backend unless `RECERTIA_API_ALLOW_LOCAL_EXEC=1`
is set (break-glass). Pytest already sets that in `tests/conftest.py`.
`RECERTIA_DEPLOYMENT_MODE=multi_node` refuses SQLite and the local backend
([ADR-0021](docs/adr/0021-deployment-model.md)); development stays `single_node`.

## Checks before you send a change

These match [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```bash
ruff check contracts/ src/ scripts/ tests/ conftest.py
mypy contracts/ src/recertia/
python3 scripts/generate_schemas.py --check
python3 scripts/export_examples.py --check
recertia skills lint --skills-root skills
python3 scripts/check_cross_refs.py --check
python3 scripts/check_milestone_deps.py --check
python3 scripts/check_assumptions_hygiene.py --check
python3 scripts/security_review.py --check
pytest -v
```

If you change a model in `contracts/`, regenerate schemas:

```bash
python3 scripts/generate_schemas.py
```

Do not hand-edit files under `schema/`.

## Research artifacts

Spreadsheets and scored JSON under [`research/`](research/) are ordinary git
files (a few hundred KB). You do **not** need Git LFS to clone or work on this
repo. Markdown notes and bibliography extracts in that tree are also plain git.

## What belongs in a PR

- Keep diffs on-task. Do not rewrite archived Q3 plans or mark research assumptions
  in [`docs/assumptions.md`](docs/assumptions.md) `supported` from CI alone.
- Default T2 policy is [`policy/default.json`](policy/default.json). Weekly job spend
  belongs in `{runs_root}/jobs/job_quota.sqlite` (JSON sidecar
  `{runs_root}/jobs/job_quota.json` is the operator export), not in that file.
- HEX/compress flags stay off unless the enablement predicates in
  [`docs/architecture/remaining-work.md`](docs/architecture/remaining-work.md) hold.

Cursor Cloud / coding-agent notes (not this guide) are in [`AGENTS.md`](AGENTS.md). Architecture and
specs are indexed from [`docs/architecture.md`](docs/architecture.md) and
[`docs/specifications.md`](docs/specifications.md).
