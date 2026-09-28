"""The app's main actions, kept separate from printing (CLI) and HTTP (web server) so both can reuse them."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .arxiv_client import ArxivClient, ArxivError, SearchPage, normalize_arxiv_id
from .ingest import IngestResult, ingest_paper, pdf_files_for
from .storage import FAILED, IN_PROGRESS, QUEUED, Library, SaveReport, now_iso

LAST_SEARCH_KEY = "last_arxiv_search_at"

# Outcomes for IDs that could not be selected at all (FAILED is also a stored status).
INVALID_ID = "invalid_id"
NOT_FOUND = "not_found"


@dataclass
class SearchOutcome:
    page: SearchPage
    saved: SaveReport


@dataclass
class Selection:
    ready: list[str] = field(default_factory=list)  # in the collection and ready to ingest
    problems: list[IngestResult] = field(default_factory=list)  # invalid, not found, or lookup failed


def search_and_save(
    client: ArxivClient, library: Library, query: str, start: int = 0, max_results: int = 10, sort: str = "relevance"
) -> SearchOutcome:
    """Search arXiv and cache every result's metadata (it joins the collection only when added).

    The fetch happens before anything is written, so if arXiv fails (ArxivError is raised)
    the library is left exactly as it was: a failed refresh never removes saved papers.
    """
    page = client.search(query, start=start, max_results=max_results, sort=sort)
    saved = library.save_papers(page.papers)
    library.set_meta(LAST_SEARCH_KEY, now_iso())
    return SearchOutcome(page=page, saved=saved)


def select_papers(client: ArxivClient, library: Library, raw_ids: list[str]) -> Selection:
    """Add papers to the collection by ID and mark them queued for ingestion.

    Papers not yet in the library (e.g. an ID the user found elsewhere) are looked up on arXiv.
    """
    selection = Selection()
    wanted: list[str] = []
    for raw in raw_ids:
        try:
            arxiv_id = normalize_arxiv_id(raw)
        except ValueError as exc:
            selection.problems.append(IngestResult(raw, INVALID_ID, str(exc)))
            continue
        if arxiv_id not in wanted:
            wanted.append(arxiv_id)

    unknown = [i for i in wanted if library.get_paper(i) is None]
    if unknown:
        try:
            library.save_papers(client.fetch_by_ids(unknown))
        except ArxivError as exc:
            for arxiv_id in unknown:
                selection.problems.append(IngestResult(arxiv_id, FAILED, f"could not look up metadata: {exc}"))
            wanted = [i for i in wanted if i not in unknown]

    for arxiv_id in wanted:
        stored = library.get_paper(arxiv_id)
        if stored is None:
            selection.problems.append(IngestResult(arxiv_id, NOT_FOUND, "arXiv has no paper with this ID"))
        else:
            selection.ready.append(arxiv_id)
    library.add_to_collection(selection.ready)
    return selection


def queue_for_ingest(library: Library, arxiv_ids: list[str]) -> None:
    for arxiv_id in arxiv_ids:
        library.set_state(arxiv_id, QUEUED, "Waiting to download")


def ingest_by_id(client: ArxivClient, library: Library, arxiv_id: str, pdf_dir: Path, force: bool = False) -> IngestResult:
    stored = library.get_paper(arxiv_id)
    if stored is None:
        return IngestResult(arxiv_id, NOT_FOUND, "this paper is not in the library")
    return ingest_paper(library, stored.paper, client.download, pdf_dir, force=force)


def add_papers(
    client: ArxivClient, library: Library, raw_ids: list[str], pdf_dir: Path, force: bool = False
) -> list[IngestResult]:
    """Select papers and ingest them one after another (used by the command line and the evaluation).

    Returns one result per requested ID. One paper failing never stops the others.
    """
    selection = select_papers(client, library, raw_ids)
    results = list(selection.problems)
    for arxiv_id in selection.ready:
        results.append(ingest_by_id(client, library, arxiv_id, pdf_dir, force=force))
    return results


@dataclass
class Removal:
    arxiv_id: str
    title: str
    chunks_deleted: int
    pdfs_deleted: int


def remove_paper(library: Library, arxiv_id: str, pdf_dir: Path) -> Removal:
    """Delete a paper's metadata, indexed chunks and downloaded PDFs from this computer."""
    stored = library.get_paper(arxiv_id)
    if stored is None:
        raise KeyError(arxiv_id)
    if stored.ingest_status in IN_PROGRESS:
        raise ValueError("This paper is still being ingested; wait until it finishes, then remove it.")
    pdfs = pdf_files_for(pdf_dir, arxiv_id)
    chunks = library.delete_paper(arxiv_id)
    for pdf in pdfs:
        pdf.unlink(missing_ok=True)
    return Removal(arxiv_id, stored.paper.title, chunks, len(pdfs))
