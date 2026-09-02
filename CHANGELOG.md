# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **CI architecture2 drift** — regenerate `docs/architecture2.md` so the Recuris
  `a10` assumption from #47/#48 is compiled. `generate_architecture2.py --check`
  is green again.
- **weekly-ops postgres-soak** — use `pgvector/pgvector:pg16` instead of
  `postgres:16-alpine`. Soak migrations call `CREATE EXTENSION vector`; the
  stock image does not ship it, so every Monday job failed.
