"""Optional arXiv PDF download + text extract for paper ingest.

Network isolation for the *execution* sandbox stays intact (``network=none``).
PDF bytes are fetched on the improvement-plane host, then either:

* extracted on the host via optional ``pypdf``, or
* copied into a workdir and extracted by a sandboxed Python script when the
  operator has installed ``pypdf`` in an allowlisted image (``RECERTIA_ALLOW_CUSTOM_IMAGE``).

Default is host-side optional extract. Missing ``pypdf`` yields empty text, not an error.
"""

from __future__ import annotations

import os
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from recertia.jobs.arxiv import DEFAULT_USER_AGENT, ArxivPaper


class ArxivPdfError(RuntimeError):
    """PDF download or extract failure."""


@dataclass(frozen=True)
class PdfExtractResult:
    pdf_path: Path | None
    text: str
    method: str  # "pypdf-host" | "pypdf-sandbox" | "skipped" | "download-only"
    chars: int


def download_pdf(
    paper: ArxivPaper,
    dest_dir: Path,
    *,
    timeout_s: float = 60.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Path:
    """Download PDF bytes to ``dest_dir / <arxiv_id>.pdf``."""

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_id = paper.arxiv_id.replace("/", "_")
    dest = dest_dir / f"{safe_id}.pdf"
    url = paper.pdf_url or f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/pdf"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise ArxivPdfError(f"PDF HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ArxivPdfError(f"PDF network error: {exc.reason}") from exc
    if len(data) < 100 or not data.startswith(b"%PDF"):
        raise ArxivPdfError(f"PDF payload does not look like a PDF ({len(data)} bytes)")
    dest.write_bytes(data)
    return dest


def extract_text_host(pdf_path: Path, *, max_chars: int = 50_000) -> str:
    """Extract text with ``pypdf`` if installed; otherwise return empty string."""

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(pdf_path))
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            if not text.strip():
                continue
            chunks.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n".join(chunks)[:max_chars]
    except Exception:
        return ""


def extract_text_in_sandbox(
    pdf_path: Path,
    *,
    workdir: Path,
    max_chars: int = 50_000,
    timeout_s: int = 120,
) -> str:
    """Copy PDF into workdir and attempt extract inside the configured backend.

    Requires ``pypdf`` inside the container image. Standard allowlisted slim images
    do not ship it — set a custom image with ``RECERTIA_ALLOW_CUSTOM_IMAGE=1`` or
    prefer :func:`extract_text_host`.
    """

    from recertia.solver.container import run_configured_command

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / pdf_path.name
    if target.resolve() != pdf_path.resolve():
        target.write_bytes(pdf_path.read_bytes())
    script = workdir / "_extract_pdf.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            try:
                from pypdf import PdfReader
            except ImportError:
                print("")
                raise SystemExit(0)
            reader = PdfReader({target.name!r})
            chunks = []
            total = 0
            limit = {int(max_chars)}
            for page in reader.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    continue
                if not t.strip():
                    continue
                chunks.append(t)
                total += len(t)
                if total >= limit:
                    break
            print("\\n".join(chunks)[:limit])
            """
        ),
        encoding="utf-8",
    )
    proc = run_configured_command(
        f"python {script.name}",
        workdir=workdir,
        timeout_s=timeout_s,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "")[:max_chars]


def fetch_and_extract(
    paper: ArxivPaper,
    dest_dir: Path,
    *,
    extract: bool = True,
    use_sandbox: bool = False,
    sandbox_workdir: Path | None = None,
    max_chars: int = 50_000,
) -> PdfExtractResult:
    """Download PDF; optionally extract text (host or sandbox)."""

    enabled = os.environ.get("RECERTIA_PDF_EXTRACT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    pdf_path = download_pdf(paper, dest_dir)
    if not extract or not enabled:
        return PdfExtractResult(
            pdf_path=pdf_path, text="", method="download-only", chars=0
        )
    if use_sandbox:
        workdir = Path(sandbox_workdir or dest_dir / "_sandbox")
        text = extract_text_in_sandbox(pdf_path, workdir=workdir, max_chars=max_chars)
        method = "pypdf-sandbox" if text else "skipped"
        return PdfExtractResult(pdf_path=pdf_path, text=text, method=method, chars=len(text))
    text = extract_text_host(pdf_path, max_chars=max_chars)
    method = "pypdf-host" if text else "skipped"
    return PdfExtractResult(pdf_path=pdf_path, text=text, method=method, chars=len(text))
