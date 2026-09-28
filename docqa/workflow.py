"""The app's main actions, kept separate from printing so they are easy to test."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .arxiv_client import ArxivClient, ArxivError, SearchPage, normalize_arxiv_id
from .ingest import IngestResult, ingest_paper
from .storage import Library, SaveReport, now_iso

# Extra outcomes for IDs that could not be ingested at all.
INVALID_ID = "invalid_id"
NOT_FOUND = "not_found"
FAILED = "failed"

LAST_SEARCH_KEY = "last_arxiv_search_at"


@dataclass
class SearchOutcome:
    page: SearchPage
    saved: SaveReport


def search_and_save(
    client: ArxivClient, library: Library, query: str, start: int = 0, max_results: int = 10, sort: str = "relevance"
) -> SearchOutcome:
    """Search arXiv and save every result's metadata.

    The fetch happens before anything is written, so if arXiv fails (ArxivError is raised)
    the library is left exactly as it was: a failed refresh never removes saved papers.
    """
    page = client.search(query, start=start, max_results=max_results, sort=sort)
    saved = library.save_papers(page.papers)
    library.set_meta(LAST_SEARCH_KEY, now_iso())
    return SearchOutcome(page=page, saved=saved)


def add_papers(
    client: ArxivClient, library: Library, raw_ids: list[str], pdf_dir: Path, force: bool = False
) -> list[IngestResult]:
    """Select papers by ID and ingest each one. Returns one result per requested ID.

    Papers not yet in the library (e.g. an ID the user found elsewhere) are looked up on
    arXiv first. One paper failing never stops the others.
    """
    results: list[IngestResult] = []
    wanted: list[str] = []
    for raw in raw_ids:
        try:
            arxiv_id = normalize_arxiv_id(raw)
        except ValueError as exc:
            results.append(IngestResult(raw, INVALID_ID, str(exc)))
            continue
        if arxiv_id not in wanted:
            wanted.append(arxiv_id)

    unknown = [i for i in wanted if library.get_paper(i) is None]
    if unknown:
        try:
            library.save_papers(client.fetch_by_ids(unknown))
        except ArxivError as exc:
            for arxiv_id in unknown:
                results.append(IngestResult(arxiv_id, FAILED, f"could not look up metadata: {exc}"))
            wanted = [i for i in wanted if i not in unknown]

    def fetch_pdf(url: str) -> bytes:
        return client.get(url).content

    for arxiv_id in wanted:
        stored = library.get_paper(arxiv_id)
        if stored is None:
            results.append(IngestResult(arxiv_id, NOT_FOUND, "arXiv has no paper with this ID"))
            continue
        try:
            results.append(ingest_paper(library, stored.paper, fetch_pdf, pdf_dir, force=force))
        except OSError as exc:  # e.g. disk full or no permission to write the PDF
            results.append(IngestResult(arxiv_id, FAILED, f"could not save files: {exc}"))
    return results
