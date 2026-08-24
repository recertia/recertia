# arXiv paper ingestion (Miner)

**Status:** shipped on the improvement plane as an offline **mine** path.
**Rule:** proposals only. No approved writes. No weight updates. No LLM extraction inside the job.

## What it does

1. Fetches Atom metadata from `export.arxiv.org` (ids or `search_query`).
2. Emits `Proposal(kind="mine")` rows with `payload.curation = "mined_from_paper"`.
3. Optionally downloads the PDF on the **host** (execution sandbox stays `network=none`).
4. Optionally extracts text via `pypdf` (host, or sandboxed if the image has it).
5. Optionally **distills** a pitfall-oriented candidate skill + semantic facts keyed by `arxiv_id`.
6. `--submit` materialises **candidates** only; promotion stays behind the golden gate.

## CLI

```bash
# dry-run proposals for specific papers
recertia jobs run mine --arxiv-id 2605.22148 --arxiv-id 2607.01120 --dry-run

# search (max 50)
recertia jobs run mine --arxiv-query 'ti:"self-evolving" AND cat:cs.AI' --arxiv-max 5 --dry-run

# PDF download + optional extract (requires pypdf for text)
recertia jobs run mine --arxiv-id 2605.22148 --with-pdf --dry-run

# distill pitfall skill + write candidates + facts
recertia jobs run mine --arxiv-id 2605.22148 --distill-paper --submit \
  --facts-root .recertia/facts
```

Human-artifact mining is unchanged:

```bash
recertia jobs run mine --hint "docs/ops/runbook.md" --submit
```

## Distill heuristics (deterministic)

`recertia.distill.paper.distill_paper`:

* splits the abstract into sentences
* scores **pitfalls** on cues (`fail`, `without`, `bottleneck`, `bias`, `unbounded`, …)
* scores **claims** on cues (`we show`, `propose`, `measure`, `lift`, …)
* authors `failure_modes` + bounded shell steps under the authoring prior
* writes `Fact` rows with slugs `arxiv-<id>-meta|claim-N|pitfall-N|pdf-…`

No LLM. Golden promotion is still required before retrieval trust rises.

## PDF extract

| Path | Network | Text |
| --- | --- | --- |
| Host download + optional `pypdf` | improvement-plane host | yes if `pypdf` installed |
| Sandbox extract (`--pdf-sandbox`) | still none inside OCI | needs `pypdf` in image |

Disable extract: `RECERTIA_PDF_EXTRACT=0`.

## Contracts

`Curation` includes `mined_from_paper` (see `contracts/common.py`). Provenance on paper candidates uses that value and `derivation="mined_artifact"`.

## Honesty constraints

- Network for PDF is host-side only; execution containers stay offline.
- Rate limit: ≥3s between arXiv Atom requests (client default).
- Library growth remains capped by Curator retirement and the active-set floor.
- Lift claims for paper-derived skills still require control-arm measurement (assumptions `a1` / weekly report).
