"""Ingest one paper: download its PDF, extract and chunk the text, and index it.

If the full text can't be obtained, the paper is still ingested, but only its abstract,
and it is labelled abstract_only with the reason, so the app never claims to have read
text it doesn't have.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .arxiv_client import ArxivError, Paper
from .chunking import abstract_chunk, chunk_pages
from .pdf_text import PdfProblem, check_looks_like_pdf, clean_text, extract_pages
from .storage import ABSTRACT_ONLY, FULL_TEXT, Library

MAX_PDF_BYTES = 50 * 1024 * 1024
# Less text than this in the whole PDF means it is probably scanned images, not real text.
MIN_TEXT_CHARACTERS = 500
ALREADY_INGESTED = "already_ingested"


@dataclass(frozen=True)
class IngestResult:
    arxiv_id: str
    status: str  # FULL_TEXT, ABSTRACT_ONLY or ALREADY_INGESTED
    note: str
    page_count: int | None = None
    chunk_count: int = 0


def ingest_paper(
    library: Library,
    paper: Paper,
    fetch_pdf: Callable[[str], bytes],
    pdf_dir: Path,
    force: bool = False,
) -> IngestResult:
    stored = library.get_paper(paper.arxiv_id)
    if stored is None:
        raise ValueError(f"{paper.arxiv_id} must be saved in the library before it is ingested.")
    if not force and stored.ingest_status == FULL_TEXT and stored.ingested_version == paper.version:
        return IngestResult(
            paper.arxiv_id, ALREADY_INGESTED, f"full text of {paper.version} is already indexed",
            stored.page_count, library.chunk_count(paper.arxiv_id),
        )

    pdf_path = pdf_dir / f"{paper.arxiv_id.replace('/', '_')}{paper.version}.pdf"
    try:
        data = _load_or_download(pdf_path, paper.pdf_url, fetch_pdf, force)
        pages = [clean_text(page) for page in extract_pages(data)]
    except (ArxivError, PdfProblem) as exc:
        return _ingest_abstract_only(library, paper, f"full text unavailable: {exc}")

    if sum(len(page) for page in pages) < MIN_TEXT_CHARACTERS:
        return _ingest_abstract_only(
            library, paper, "full text unavailable: the PDF has almost no extractable text (it may be scanned images)"
        )

    chunks = chunk_pages(paper.arxiv_id, pages)
    empty_pages = sum(1 for page in pages if not page)
    note = f"{len(pages)} pages" + (f", {empty_pages} without extractable text" if empty_pages else "")
    library.record_ingest(paper.arxiv_id, FULL_TEXT, note, chunks, paper.version, str(pdf_path), len(pages))
    return IngestResult(paper.arxiv_id, FULL_TEXT, note, len(pages), len(chunks))


def _load_or_download(pdf_path: Path, url: str, fetch_pdf: Callable[[str], bytes], force: bool) -> bytes:
    """Reuse a PDF downloaded earlier; otherwise download it and save it."""
    if pdf_path.exists() and not force:
        data = pdf_path.read_bytes()
        check_looks_like_pdf(data, MAX_PDF_BYTES)
        return data
    data = fetch_pdf(url)
    check_looks_like_pdf(data, MAX_PDF_BYTES)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    partial = pdf_path.with_suffix(".part")
    partial.write_bytes(data)
    os.replace(partial, pdf_path)  # rename at the end so a crash never leaves a half-written PDF
    return data


def _ingest_abstract_only(library: Library, paper: Paper, reason: str) -> IngestResult:
    chunk = abstract_chunk(paper.arxiv_id, paper.title, paper.summary)
    library.record_ingest(paper.arxiv_id, ABSTRACT_ONLY, reason, [chunk], paper.version)
    return IngestResult(paper.arxiv_id, ABSTRACT_ONLY, reason, None, 1)
