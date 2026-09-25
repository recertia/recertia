"""Deterministic arXiv Atom client for Miner paper ingestion.

Fetches metadata only (title, abstract, authors, categories, links).
Does not download PDFs and does not call an LLM. Extraction of reusable
skills remains a later distill/review step gated by the golden set.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
OPENSEARCH_NS = "{http://a9.com/-/spec/opensearch/1.1/}"

ARXIV_API = "http://export.arxiv.org/api/query"
DEFAULT_USER_AGENT = "RecertiaArxivIngest/0.1 (https://github.com/recertia/recertia; research)"
# arXiv asks for polite spacing between requests.
MIN_REQUEST_INTERVAL_S = 3.0

_ID_RE = re.compile(
    r"^(?:arXiv:)?(?P<id>\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    abstract: str
    authors: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    published: str | None = None
    updated: str | None = None
    pdf_url: str | None = None
    abs_url: str | None = None
    primary_category: str | None = None
    comment: str | None = None

    def skill_id_slug(self) -> str:
        """Stable skill_id fragment matching contracts.skill._SKILL_ID_PATTERN."""

        core = self.arxiv_id.lower().replace("/", "-").replace(".", "-")
        core = re.sub(r"[^a-z0-9\-]+", "-", core)
        core = re.sub(r"-+", "-", core).strip("-")
        return f"arxiv-{core}"


@dataclass
class ArxivClient:
    """Thin Atom client. Inject a custom opener in tests."""

    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: float = 30.0
    min_interval_s: float = MIN_REQUEST_INTERVAL_S
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def fetch_by_ids(self, arxiv_ids: Iterable[str]) -> list[ArxivPaper]:
        ids = [normalize_arxiv_id(x) for x in arxiv_ids]
        ids = [i for i in ids if i]
        if not ids:
            return []
        # Batch in chunks of 20 (API practical limit for id_list).
        out: list[ArxivPaper] = []
        for i in range(0, len(ids), 20):
            chunk = ids[i : i + 20]
            params = {
                "id_list": ",".join(chunk),
                "max_results": str(len(chunk)),
            }
            out.extend(self._query(params))
        return out

    def search(
        self,
        query: str,
        *,
        start: int = 0,
        max_results: int = 5,
        sort_by: str = "relevance",
        sort_order: str = "descending",
    ) -> list[ArxivPaper]:
        q = query.strip()
        if not q:
            return []
        max_results = max(1, min(int(max_results), 50))
        params = {
            "search_query": q,
            "start": str(max(0, start)),
            "max_results": str(max_results),
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }
        return self._query(params)

    def _query(self, params: dict[str, str]) -> list[ArxivPaper]:
        self._throttle()
        url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/atom+xml",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise ArxivFetchError(f"arXiv HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise ArxivFetchError(f"arXiv network error: {exc.reason}") from exc
        self._last_request_at = time.monotonic()
        return parse_atom_feed(body)

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)


class ArxivFetchError(RuntimeError):
    """Network or protocol failure talking to export.arxiv.org."""


def normalize_arxiv_id(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    m = _ID_RE.match(text)
    if not m:
        # Accept bare new-style ids without version.
        if re.fullmatch(r"\d{4}\.\d{4,5}", text):
            return text
        return ""
    return m.group("id")


def parse_atom_feed(body: bytes | str) -> list[ArxivPaper]:
    if isinstance(body, str):
        body = body.encode("utf-8")
    root = ET.fromstring(body)
    papers: list[ArxivPaper] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        paper = _parse_entry(entry)
        if paper is not None:
            papers.append(paper)
    return papers


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return " ".join(el.text.split())


def _parse_entry(entry: ET.Element) -> ArxivPaper | None:
    id_text = _text(entry.find(f"{ATOM_NS}id"))
    # Typical: http://arxiv.org/abs/2605.22148v1
    arxiv_id = ""
    if "/abs/" in id_text:
        arxiv_id = id_text.rsplit("/abs/", 1)[-1]
    arxiv_id = normalize_arxiv_id(arxiv_id) or normalize_arxiv_id(id_text)
    if not arxiv_id:
        return None

    title = _text(entry.find(f"{ATOM_NS}title"))
    abstract = _text(entry.find(f"{ATOM_NS}summary"))
    authors = tuple(
        _text(a.find(f"{ATOM_NS}name"))
        for a in entry.findall(f"{ATOM_NS}author")
        if _text(a.find(f"{ATOM_NS}name"))
    )
    categories = tuple(
        (c.get("term") or "").strip()
        for c in entry.findall(f"{ATOM_NS}category")
        if (c.get("term") or "").strip()
    )
    primary = entry.find(f"{ARXIV_NS}primary_category")
    primary_category = (primary.get("term") if primary is not None else None) or (
        categories[0] if categories else None
    )
    published = _text(entry.find(f"{ATOM_NS}published")) or None
    updated = _text(entry.find(f"{ATOM_NS}updated")) or None
    comment = _text(entry.find(f"{ARXIV_NS}comment")) or None

    pdf_url = None
    abs_url = None
    for link in entry.findall(f"{ATOM_NS}link"):
        rel = link.get("rel") or ""
        href = link.get("href") or ""
        title_attr = link.get("title") or ""
        if title_attr == "pdf" or (rel == "related" and href.endswith(".pdf")):
            pdf_url = href
        elif rel in ("", "alternate") and "/abs/" in href:
            abs_url = href
    if abs_url is None:
        abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    if pdf_url is None:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title or f"arXiv:{arxiv_id}",
        abstract=abstract,
        authors=authors,
        categories=categories,
        published=published,
        updated=updated,
        pdf_url=pdf_url,
        abs_url=abs_url,
        primary_category=primary_category,
        comment=comment,
    )


def paper_to_payload(paper: ArxivPaper) -> dict:
    """JSON-serialisable payload for Proposal and candidate provenance."""

    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": list(paper.authors),
        "categories": list(paper.categories),
        "primary_category": paper.primary_category,
        "published": paper.published,
        "updated": paper.updated,
        "pdf_url": paper.pdf_url,
        "abs_url": paper.abs_url,
        "comment": paper.comment,
        "curation": "mined_from_paper",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
