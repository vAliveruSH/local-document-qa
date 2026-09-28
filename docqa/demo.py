"""Public, read-only demo of the app: ask questions about a small bundled set of openly licensed papers.

This is a separate app from docqa/server.py (the full local app), which it does not use.
It registers only four API routes:

    GET  /api/status        GET  /api/papers        GET  /api/papers/{id}        POST /api/ask

There is no route for searching arXiv, adding, ingesting, retrying or removing papers, and
no answer model is ever created, so none of those can be triggered, whatever a client sends.
The bundled database is opened read-only (see Library.open_read_only) as a second layer.

Files it serves (default folder `demo/`, override with DOCQA_DEMO_DIR):
    demo/library.db    the bundled library (built by a separate script, never at runtime)
    demo/corpus.json   licence and attribution for every paper in library.db

Local preview:  python -m uvicorn docqa.demo:app --port 8001
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .answer import answer_question
from .arxiv_client import normalize_arxiv_id
from .storage import FULL_TEXT, Library, StoredPaper

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_REPO_URL = "https://github.com/vAliveruSH/local-document-qa"

MAX_QUESTION_CHARS = 500
MAX_PASSAGES = 10
ASKS_PER_MINUTE_PER_VISITOR = 20
ASKS_PER_MINUTE_PER_INSTANCE = 300

# Licences whose terms allow publishing the extracted text with attribution.
ALLOWED_LICENSES = {
    "https://creativecommons.org/licenses/by/4.0/": "CC BY 4.0",
    "https://creativecommons.org/publicdomain/zero/1.0/": "CC0 1.0",
}
CHANGES_NOTE = (
    "Text extracted from the PDF and split into passages; figures, tables and equations are omitted or altered; "
    "author email addresses removed."
)

# Author emails printed in papers: plain (name@uni.edu) and grouped ({a, b}@uni.edu).
EMAIL_ADDRESS = re.compile(r"\{[^{}@]{1,200}\}\s*@\s*[\w-]+(?:\.[\w-]+)+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_REMOVED = "[email removed]"


def remove_email_addresses(text: str) -> str:
    return EMAIL_ADDRESS.sub(EMAIL_REMOVED, text)


def demo_dir() -> Path:
    return Path(os.environ.get("DOCQA_DEMO_DIR", config.PROJECT_ROOT / "demo"))


class DemoUnavailable(Exception):
    """The bundled corpus is missing or not safe to publish."""


# ---- the corpus: database + licence manifest, checked together -------------------------------

@dataclass(frozen=True)
class CorpusEntry:
    arxiv_id: str
    version: str
    license_name: str
    license_url: str
    license_evidence: str  # the arXiv page for this exact version, which shows the licence

    def as_json(self) -> dict:
        return {
            "name": self.license_name,
            "url": self.license_url,
            "evidence": self.license_evidence,
            "changes": CHANGES_NOTE,
        }


def load_manifest(path: Path) -> dict[str, CorpusEntry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = {}
        for item in data["papers"]:
            url = item["license_url"]
            if url not in ALLOWED_LICENSES:
                raise DemoUnavailable(f"{item['arxiv_id']} has a licence that is not allowed in the demo")
            entry = CorpusEntry(
                arxiv_id=item["arxiv_id"],
                version=item["version"],
                license_name=ALLOWED_LICENSES[url],
                license_url=url,
                license_evidence=f"https://arxiv.org/abs/{item['arxiv_id']}{item['version']}",
            )
            entries[entry.arxiv_id] = entry
        return entries
    except FileNotFoundError as exc:
        raise DemoUnavailable("The demo corpus has not been built yet.") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoUnavailable("The demo corpus manifest is invalid.") from exc


def check_corpus(library: Library, manifest: dict[str, CorpusEntry]) -> None:
    """Refuse to serve anything unless every paper in the database is licensed for publishing."""
    papers = library.list_papers()
    if not papers:
        raise DemoUnavailable("The demo database is empty.")
    if {s.paper.arxiv_id for s in papers} != set(manifest):
        raise DemoUnavailable("The demo database and its licence manifest list different papers.")
    for stored in papers:
        entry = manifest.get(stored.paper.arxiv_id)
        if entry is None or entry.version != stored.paper.version:
            raise DemoUnavailable("The demo database contains a paper without a matching licence entry.")
        if stored.ingest_status != FULL_TEXT or stored.pdf_path:
            raise DemoUnavailable("The demo database was not built for publishing.")
        if any(EMAIL_ADDRESS.search(chunk.text) for chunk in library.get_chunks(stored.paper.arxiv_id)):
            raise DemoUnavailable("The demo database was not built for publishing.")


def bundle_database(source: Path, target: Path) -> None:
    """Copy a library into a single self-contained file that is safe to ship read-only.

    Merges any WAL data, drops local file paths and cached search results, removes author email
    addresses from the passages and the search index, and switches to a rollback journal so no
    -wal/-shm side files are needed. Used by the corpus build script.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with closing(sqlite3.connect(source)) as src:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src.execute("VACUUM INTO ?", (str(target),))
    with closing(sqlite3.connect(target)) as out:
        out.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT c.chunk_id FROM chunks c "
                    "JOIN papers p ON p.arxiv_id = c.arxiv_id WHERE p.in_collection = 0)")
        out.execute("DELETE FROM chunks WHERE arxiv_id IN (SELECT arxiv_id FROM papers WHERE in_collection = 0)")
        out.execute("DELETE FROM papers WHERE in_collection = 0")
        out.execute("UPDATE papers SET pdf_path = '', progress = NULL")
        out.execute("DELETE FROM meta")
        for chunk_id, text in out.execute("SELECT chunk_id, text FROM chunks").fetchall():
            cleaned = remove_email_addresses(text)
            if cleaned != text:
                out.execute("UPDATE chunks SET text = ? WHERE chunk_id = ?", (cleaned, chunk_id))
                out.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
                out.execute("INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)", (chunk_id, cleaned))
        out.commit()
        out.execute("PRAGMA journal_mode = DELETE")
        out.execute("VACUUM")


# ---- abuse controls ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding one-minute window per visitor, plus a cap for the whole server instance.

    Kept in memory, so it is per instance: a host that runs several copies has several limiters.
    """

    def __init__(self, per_visitor: int, per_instance: int, window: float = 60.0, clock=time.monotonic):
        self.per_visitor, self.per_instance, self.window, self.clock = per_visitor, per_instance, window, clock
        self._visitors: dict[str, deque] = {}
        self._all: deque = deque()
        self._lock = threading.Lock()

    def retry_after(self, visitor: str) -> float:
        """0 if the request may go ahead (and records it); otherwise seconds to wait."""
        now = self.clock()
        with self._lock:
            if len(self._visitors) > 10_000:  # don't let many visitors grow memory without bound
                self._visitors.clear()
            hits = self._visitors.setdefault(visitor, deque())
            for queue in (hits, self._all):
                while queue and now - queue[0] >= self.window:
                    queue.popleft()
            for queue, limit in ((hits, self.per_visitor), (self._all, self.per_instance)):
                if len(queue) >= limit:
                    return max(1.0, self.window - (now - queue[0]))
            hits.append(now)
            self._all.append(now)
            return 0.0


def visitor_key(request: Request) -> str:
    # On Vercel, x-forwarded-for is set by Vercel itself ("we currently overwrite the X-Forwarded-For
    # header ... to prevent IP spoofing"). Anywhere else a client could fake it, so it is ignored.
    if os.environ.get("VERCEL") == "1":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def content_security_policy() -> str:
    """Only this site's own files may run; the one inline script (theme) is allowed by its hash."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(s.encode('utf-8')).digest()).decode()}'"
        for s in re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' {hashes}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


# ---- request / response shapes -------------------------------------------------------------

class DemoAskRequest(BaseModel):
    # extra="forbid": unknown fields (for example "generate": true) are rejected with 422.
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    paper_id: str | None = Field(default=None, max_length=40)
    top_k: int = Field(default=5, ge=1, le=MAX_PASSAGES)


def demo_paper_json(library: Library, stored: StoredPaper, entry: CorpusEntry) -> dict:
    paper = stored.paper
    return {
        "arxiv_id": paper.arxiv_id,
        "version": paper.version,
        "title": paper.title,
        "authors": list(paper.authors),
        "summary": paper.summary,
        "published": paper.published,
        "updated": paper.updated,
        "abs_url": paper.abs_url,
        "pdf_url": paper.pdf_url,
        "primary_category": paper.primary_category,
        "categories": list(paper.categories),
        "in_collection": True,
        "status": stored.ingest_status,
        "note": "",
        "progress": None,
        "page_count": stored.page_count,
        "chunk_count": library.chunk_count(paper.arxiv_id),
        "ingested_version": stored.ingested_version,
        "newer_version_available": False,
        "first_saved_at": "",
        "ingested_at": "",
        "license": entry.as_json(),
    }


# ---- the app ----------------------------------------------------------------------------------

def create_demo_app(demo_folder: Path | None = None, repo_url: str | None = None) -> FastAPI:
    folder = Path(demo_folder) if demo_folder else demo_dir()
    db_path, manifest_path = folder / "library.db", folder / "corpus.json"
    repo_url = repo_url or os.environ.get("DOCQA_DEMO_REPO_URL", DEFAULT_REPO_URL)
    limiter = RateLimiter(ASKS_PER_MINUTE_PER_VISITOR, ASKS_PER_MINUTE_PER_INSTANCE)
    csp = content_security_policy()
    cache: dict = {}

    # No interactive API docs: the demo exposes only what the page needs.
    app = FastAPI(title="Local Document Q&A (public demo)", docs_url=None, redoc_url=None, openapi_url=None)

    def corpus() -> dict[str, CorpusEntry]:
        """Load and check the corpus once per process (the files never change while running)."""
        if "manifest" not in cache:
            manifest = load_manifest(manifest_path)
            try:
                with open_library() as library:
                    check_corpus(library, manifest)
            except (FileNotFoundError, sqlite3.DatabaseError) as exc:
                raise DemoUnavailable("The demo database is missing or unreadable.") from exc
            cache["manifest"] = manifest
        return cache["manifest"]

    def open_library() -> Library:
        return Library.open_read_only(db_path)

    def require_corpus() -> dict[str, CorpusEntry]:
        try:
            return corpus()
        except DemoUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def hide_internal_errors(request: Request, exc: Exception):
        # Never send exception text (which could contain server paths) to visitors.
        return JSONResponse({"detail": "Something went wrong on the server."}, status_code=500)

    @app.get("/api/status")
    def status():
        try:
            manifest, problem = corpus(), ""
        except DemoUnavailable as exc:
            manifest, problem = {}, str(exc)
        stats = {"papers": 0, "indexed": 0, "full_text": 0, "abstract_only": 0, "in_progress": 0,
                 "failed": 0, "metadata_only": 0, "chunks": 0, "cached_search_results": 0}
        if manifest:
            with open_library() as library:
                stats = library.stats()
        return {
            "mode": "demo",
            "demo": {
                "available": not problem,
                "problem": problem,
                "repo_url": repo_url,
                "limits": {
                    "max_question_chars": MAX_QUESTION_CHARS,
                    "max_passages": MAX_PASSAGES,
                    "asks_per_minute": ASKS_PER_MINUTE_PER_VISITOR,
                },
            },
            "stats": stats,
            "last_search_at": None,
            "arxiv": {"state": "disabled", "message": "Searching arXiv is turned off in the public demo", "at": ""},
            "index": {"engine": "SQLite FTS5 · BM25", "chunks": stats["chunks"]},
            "model": {"available": False, "name": "", "detail": "No answer model runs in the public demo"},
        }

    @app.get("/api/papers")
    def list_papers():
        manifest = require_corpus()
        with open_library() as library:
            papers = [demo_paper_json(library, s, manifest[s.paper.arxiv_id]) for s in library.list_papers()]
            return {"stats": library.stats(), "papers": papers}

    @app.get("/api/papers/{arxiv_id:path}")
    def get_paper(arxiv_id: str):
        manifest = require_corpus()
        paper_id = known_paper_id(arxiv_id, manifest)
        with open_library() as library:
            return demo_paper_json(library, library.get_paper(paper_id), manifest[paper_id])

    @app.post("/api/ask")
    def ask(body: DemoAskRequest, request: Request):
        wait = limiter.retry_after(visitor_key(request))
        if wait:
            raise HTTPException(
                429, "Too many questions in a short time. Please wait a minute and try again.",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        manifest = require_corpus()
        paper_id = known_paper_id(body.paper_id, manifest) if body.paper_id else None
        with open_library() as library:
            # generator=None: the demo never calls a language model.
            answer = answer_question(library, body.question, None, top_k=body.top_k, arxiv_id=paper_id)
        retrieval = answer.retrieval
        notes = list(answer.notes)
        if answer.mode == "retrieval_only":
            notes.insert(0, "Public demo: no answer model runs here, so you see passages quoted from the papers.")
        return {
            "mode": answer.mode,
            "question": retrieval.question,
            "keywords": list(retrieval.keywords),
            "missing_keywords": list(retrieval.missing_keywords),
            "enough_evidence": retrieval.enough_evidence,
            "reason": retrieval.reason,
            "answer": "",
            "generator": "",
            "notes": notes,
            "cited": [],
            "passages": [
                {
                    "rank": p.rank, "chunk_id": p.chunk_id, "arxiv_id": p.arxiv_id, "version": p.version,
                    "title": p.title, "page": p.page, "source": p.source, "location": p.location,
                    "citation": p.citation, "link": p.link, "text": p.text, "score": p.score,
                    "matched_keywords": list(p.matched_keywords), "cited": False,
                    "license": manifest[p.arxiv_id].as_json(),
                }
                for p in retrieval.passages
            ],
        }

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def known_paper_id(raw: str, manifest: dict[str, CorpusEntry]) -> str:
    try:
        paper_id = normalize_arxiv_id(raw)
    except ValueError as exc:
        raise HTTPException(400, "That is not an arXiv ID.") from exc
    if paper_id not in manifest:
        raise HTTPException(404, "That paper is not part of the demo.")
    return paper_id


# The ASGI app that hosting platforms load (e.g. Vercel: [tool.vercel] entrypoint = "docqa.demo:app").
app = create_demo_app()
