"""Talk to the arXiv API: build search queries, fetch politely, and parse the results.

arXiv's API guidance (https://info.arxiv.org/help/api/user-manual.html) asks clients to
wait at least 3 seconds between requests and to page through results with `start` and
`max_results`. ArxivClient enforces the wait and retries a few times when arXiv is busy.
"""
from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

import requests

API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "local-document-qa/0.2 (personal learning project; https://github.com/vAliveruSH/local-document-qa)"
MIN_SECONDS_BETWEEN_REQUESTS = 3.0
MAX_RESULTS_PER_REQUEST = 50
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
# 406 is included because arXiv intermittently answers valid queries with it (seen during testing).
RETRYABLE_STATUS_CODES = {406, 429, 500, 502, 503, 504}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
FIELD_PREFIX = re.compile(r"\b(ti|au|abs|co|jr|cat|rn|id|all):", re.IGNORECASE)
ID_IN_ABS_URL = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?P<version>v\d+)?$")
NEW_STYLE_ID = re.compile(r"^\d{4}\.\d{4,5}$")  # e.g. 1706.03762
OLD_STYLE_ID = re.compile(r"^[a-z][a-z.-]*/\d{7}$", re.IGNORECASE)  # e.g. hep-th/9901001


class ArxivError(Exception):
    """arXiv could not be reached, or sent back something we can't use."""


@dataclass(frozen=True)
class Paper:
    arxiv_id: str  # stable ID without version, e.g. "1706.03762"
    version: str  # e.g. "v7"
    title: str
    authors: tuple[str, ...]
    summary: str  # the abstract
    published: str  # ISO date of the first version
    updated: str  # ISO date of this version
    abs_url: str
    pdf_url: str
    primary_category: str = ""
    categories: tuple[str, ...] = ()
    comment: str = ""  # author comment, e.g. "15 pages, 5 figures"
    journal_ref: str = ""
    doi: str = ""


@dataclass(frozen=True)
class SearchPage:
    papers: list[Paper]
    total_results: int | None
    start: int
    skipped_entries: int = 0  # entries too malformed to use


def build_search_query(text: str) -> str:
    """Turn what the user typed into arXiv's search syntax.

    Plain words must all appear somewhere in the paper's metadata:
    'retrieval augmented generation' -> 'all:retrieval AND all:augmented AND all:generation'.
    Text in double quotes is kept as a phrase. If the user already wrote arXiv syntax
    (e.g. 'ti:transformer AND cat:cs.CL') it is passed through unchanged.
    """
    text = " ".join(text.split())
    if FIELD_PREFIX.search(text):
        return text
    terms = []
    for phrase, word in re.findall(r'"([^"]+)"|([\w.-]+)', text):
        if phrase.strip():
            terms.append(f'all:"{" ".join(phrase.split())}"')
        elif word.strip(".-"):
            terms.append(f"all:{word.strip('.-')}")
    if not terms:
        raise ValueError("The search query is empty. Type some words to search for.")
    return " AND ".join(terms)


def normalize_arxiv_id(text: str) -> str:
    """Accept '1706.03762', '1706.03762v7', 'arXiv:1706.03762' or an arxiv.org link; return '1706.03762'."""
    candidate = text.strip()
    candidate = re.sub(r"^https?://(www\.|export\.)?arxiv\.org/(abs|pdf)/", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"^arxiv:", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\.pdf$", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"v\d+$", "", candidate)
    if NEW_STYLE_ID.match(candidate) or OLD_STYLE_ID.match(candidate):
        return candidate
    raise ValueError(f"'{text}' does not look like an arXiv ID (expected something like 1706.03762).")


def parse_feed(xml_bytes: bytes) -> SearchPage:
    """Parse arXiv's Atom XML into Paper records. Raises ArxivError for errors or broken XML."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ArxivError(f"arXiv sent a response that is not valid XML ({exc}).") from exc
    if root.tag != f"{{{NS['atom']}}}feed":
        raise ArxivError("arXiv sent an unexpected response (not an Atom feed).")

    papers, skipped = [], 0
    for entry in root.findall("atom:entry", NS):
        entry_id = _text(entry, "atom:id")
        if "/api/errors" in entry_id:
            raise ArxivError(f"arXiv rejected the request: {_text(entry, 'atom:summary') or entry_id}")
        paper = _parse_entry(entry)
        if paper is None:
            skipped += 1
        else:
            papers.append(paper)

    return SearchPage(
        papers=papers,
        total_results=_int_or_none(_text(root, "opensearch:totalResults")),
        start=_int_or_none(_text(root, "opensearch:startIndex")) or 0,
        skipped_entries=skipped,
    )


def _parse_entry(entry: ET.Element) -> Paper | None:
    match = ID_IN_ABS_URL.search(_text(entry, "atom:id"))
    title = _clean(_text(entry, "atom:title"))
    if not match or not title:
        return None
    arxiv_id, version = match.group("id"), match.group("version") or ""

    pdf_url = ""
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = link.get("href", "").replace("http://", "https://", 1)
    primary = entry.find("arxiv:primary_category", NS)

    return Paper(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        authors=tuple(
            name for a in entry.findall("atom:author", NS) if (name := _clean(a.findtext("atom:name", "", NS)))
        ),
        summary=_clean(_text(entry, "atom:summary")),
        published=_text(entry, "atom:published"),
        updated=_text(entry, "atom:updated"),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}{version}",
        pdf_url=pdf_url or f"https://arxiv.org/pdf/{arxiv_id}{version}",
        primary_category=primary.get("term", "") if primary is not None else "",
        categories=tuple(c.get("term") for c in entry.findall("atom:category", NS) if c.get("term")),
        comment=_clean(_text(entry, "arxiv:comment")),
        journal_ref=_clean(_text(entry, "arxiv:journal_ref")),
        doi=_clean(_text(entry, "arxiv:doi")),
    )


def _text(element: ET.Element, path: str) -> str:
    return (element.findtext(path, default="", namespaces=NS) or "").strip()


def _clean(text: str) -> str:
    return " ".join(text.split())


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


class ArxivClient:
    """Makes HTTP requests to arXiv, waiting between calls and retrying when arXiv is busy."""

    def __init__(
        self,
        session: requests.Session | None = None,
        min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS,
        max_retries: int = 3,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        # One client can be shared by the web server's threads; the lock keeps the 3 s spacing global.
        self._turn_lock = threading.Lock()

    def search(self, query: str, start: int = 0, max_results: int = 10, sort: str = "relevance") -> SearchPage:
        if not 1 <= max_results <= MAX_RESULTS_PER_REQUEST:
            raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_PER_REQUEST}.")
        if start < 0:
            raise ValueError("start must be 0 or more.")
        sort_by = {"relevance": "relevance", "recent": "submittedDate"}.get(sort)
        if sort_by is None:
            raise ValueError("sort must be 'relevance' or 'recent'.")
        params = {
            "search_query": build_search_query(query),
            "start": start,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": "descending",
        }
        return parse_feed(self.get(API_URL, params).content)

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]:
        """Look up specific papers. IDs arXiv doesn't know are simply absent from the result."""
        if not arxiv_ids:
            return []
        params = {"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)}
        return parse_feed(self.get(API_URL, params).content).papers

    def download(
        self, url: str, on_progress: Callable[[int], None] | None = None, max_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> bytes:
        """Download a file (a PDF), reporting progress as a percentage when the size is known."""
        response = self.get(url, stream=True)
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        received = bytearray()
        last_reported = -1
        try:
            for piece in response.iter_content(64 * 1024):
                received.extend(piece)
                if len(received) > max_bytes:
                    raise ArxivError(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
                if on_progress and total:
                    percent = min(99, len(received) * 100 // total)
                    if percent >= last_reported + 5:  # don't report every tiny step
                        on_progress(percent)
                        last_reported = percent
        except requests.RequestException as exc:
            raise ArxivError(f"the download was interrupted ({exc.__class__.__name__})") from exc
        return bytes(received)

    def get(self, url: str, params: dict | None = None, stream: bool = False) -> requests.Response:
        """GET with the polite wait, a timeout, and retries. Raises ArxivError when it gives up."""
        attempts = self.max_retries + 1
        problem = ""
        for attempt in range(attempts):
            self._wait_for_turn()
            retry_after = 0.0
            try:
                response = self.session.get(
                    url, params=params, timeout=self.timeout, headers={"User-Agent": USER_AGENT}, stream=stream
                )
            except requests.Timeout:
                problem = f"no response within {self.timeout:.0f} seconds"
            except requests.RequestException as exc:
                problem = f"network error ({exc.__class__.__name__})"
            else:
                if response.status_code == 200:
                    return response
                problem = f"HTTP {response.status_code}"
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    raise ArxivError(f"arXiv refused the request ({problem}).")
                retry_after = _retry_after_seconds(response)
            if attempt < attempts - 1:
                # Back off: 3 s, 6 s, 12 s ... or longer if arXiv asked us to wait.
                self._sleep(max(self.min_interval * 2**attempt, retry_after))
        raise ArxivError(
            f"Could not get a response from arXiv after {attempts} attempts ({problem}). "
            "arXiv may be busy or your connection may be down; wait a minute and try again."
        )

    def _wait_for_turn(self) -> None:
        with self._turn_lock:
            if self._last_request_at is not None:
                remaining = self.min_interval - (self._clock() - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_at = self._clock()


def _retry_after_seconds(response: requests.Response) -> float:
    """Seconds arXiv asked us to wait via a Retry-After header (capped at 60 s), or 0."""
    try:
        return min(float(response.headers.get("Retry-After", "")), 60.0)
    except ValueError:
        return 0.0
