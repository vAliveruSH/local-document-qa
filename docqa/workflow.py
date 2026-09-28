"""The app's main actions, kept separate from printing so they are easy to test."""
from __future__ import annotations

from dataclasses import dataclass

from .arxiv_client import ArxivClient, SearchPage
from .storage import Library, SaveReport, now_iso

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
