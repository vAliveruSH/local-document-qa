"""Local web server: a JSON API over the same functions the command line uses, plus the web page.

Start it with `python app.py serve`. It listens on 127.0.0.1 only, so no other computer can reach it.
API documentation is generated automatically at http://127.0.0.1:8000/docs.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .answer import RETRIEVAL_ONLY, answer_question
from .arxiv_client import ArxivClient, ArxivError, normalize_arxiv_id
from .local_llm import OllamaGenerator
from .retrieval import DEFAULT_TOP_K
from .storage import FULL_TEXT, IN_PROGRESS, INDEXED, Library, StoredPaper, now_iso
from .worker import IngestWorker
from .workflow import LAST_SEARCH_KEY, queue_for_ingest, remove_paper, search_and_save, select_papers

WEB_DIR = Path(__file__).parent / "web"
MODEL_CHECK_SECONDS = 10


class AddRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)


class IngestRequest(BaseModel):
    force: bool = False


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    paper_id: str | None = None
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=20)
    generate: bool = True


def create_app(
    data_dir: Path | None = None,
    client: ArxivClient | None = None,
    model: OllamaGenerator | None = None,
) -> FastAPI:
    data_dir = Path(data_dir) if data_dir else config.data_dir()
    db_path, pdf_dir = data_dir / "library.db", data_dir / "pdfs"
    client = client or ArxivClient()  # one client for searches and downloads keeps arXiv's 3 s spacing
    model = model or OllamaGenerator()
    worker = IngestWorker(db_path, pdf_dir, client)
    arxiv_state = {"state": "unknown", "message": "Not contacted yet", "at": ""}
    model_cache = {"checked_at": 0.0, "available": False, "detail": ""}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with Library(db_path) as library:
            library.mark_interrupted()  # papers left mid-ingestion by a previous run can be retried
        worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Local Document Q&A", lifespan=lifespan)
    app.state.worker = worker

    def open_library() -> Library:
        return Library(db_path)

    def model_status() -> tuple[bool, str]:
        if time.monotonic() - model_cache["checked_at"] > MODEL_CHECK_SECONDS:
            model_cache["available"], model_cache["detail"] = model.check()
            model_cache["checked_at"] = time.monotonic()
        return model_cache["available"], model_cache["detail"]

    def record_arxiv(ok: bool, message: str) -> None:
        arxiv_state.update(state="ok" if ok else "error", message=message, at=now_iso())

    def get_stored(library: Library, arxiv_id: str) -> StoredPaper:
        try:
            arxiv_id = normalize_arxiv_id(arxiv_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        stored = library.get_paper(arxiv_id)
        if stored is None:
            raise HTTPException(404, f"{arxiv_id} is not in your library.")
        return stored

    # ---- status -------------------------------------------------------------------

    @app.get("/api/status")
    def status():
        available, detail = model_status()
        with open_library() as library:
            return {
                "stats": library.stats(),
                "last_search_at": library.get_meta(LAST_SEARCH_KEY),
                "arxiv": arxiv_state,
                "index": {"engine": "SQLite FTS5 · BM25", "chunks": library.chunk_count()},
                "model": {"available": available, "name": model.name, "detail": detail},
            }

    # ---- discover -----------------------------------------------------------------

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1, max_length=300),
        sort: str = Query("relevance", pattern="^(relevance|recent)$"),
        start: int = Query(0, ge=0),
        max: int = Query(10, ge=1, le=50),
    ):
        with open_library() as library:
            try:
                outcome = search_and_save(client, library, q, start=start, max_results=max, sort=sort)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except ArxivError as exc:
                record_arxiv(False, str(exc))
                raise HTTPException(502, str(exc)) from exc
            record_arxiv(True, "Last search succeeded")
            page = outcome.page
            return {
                "query": q,
                "start": page.start,
                "total_results": page.total_results,
                "skipped": page.skipped_entries,
                "results": [paper_json(library, library.get_paper(p.arxiv_id)) for p in page.papers],
            }

    # ---- collection ---------------------------------------------------------------

    @app.post("/api/collection")
    def add_to_collection(body: AddRequest):
        with open_library() as library:
            try:
                selection = select_papers(client, library, body.ids)
            except ArxivError as exc:
                raise HTTPException(502, str(exc)) from exc
            queued, already = [], []
            for arxiv_id in selection.ready:
                if needs_ingest(library.get_paper(arxiv_id)):
                    queued.append(arxiv_id)
                else:
                    already.append(arxiv_id)
            queue_for_ingest(library, queued)
            for arxiv_id in queued:
                worker.enqueue(arxiv_id)
            return {
                "queued": queued,
                "already_in_collection": already,
                "problems": [{"id": r.arxiv_id, "status": r.status, "note": r.note} for r in selection.problems],
            }

    @app.get("/api/papers")
    def list_papers():
        with open_library() as library:
            papers = sorted(library.list_papers(collection_only=True), key=lambda s: s.first_saved_at, reverse=True)
            return {"stats": library.stats(), "papers": [paper_json(library, s) for s in papers]}

    @app.get("/api/papers/{arxiv_id:path}")
    def get_paper(arxiv_id: str):
        with open_library() as library:
            return paper_json(library, get_stored(library, arxiv_id))

    @app.post("/api/papers/{arxiv_id:path}/ingest")
    def ingest(arxiv_id: str, body: IngestRequest | None = None):
        force = body.force if body else False
        with open_library() as library:
            stored = get_stored(library, arxiv_id)
            if stored.ingest_status in IN_PROGRESS:
                raise HTTPException(409, "This paper is already being ingested.")
            paper_id = stored.paper.arxiv_id
            library.add_to_collection([paper_id])
            queue_for_ingest(library, [paper_id])
            worker.enqueue(paper_id, force=force)
            return paper_json(library, library.get_paper(paper_id))

    @app.delete("/api/papers/{arxiv_id:path}")
    def delete_paper(arxiv_id: str):
        with open_library() as library:
            stored = get_stored(library, arxiv_id)
            try:
                removal = remove_paper(library, stored.paper.arxiv_id, pdf_dir)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {
                "arxiv_id": removal.arxiv_id,
                "title": removal.title,
                "chunks_deleted": removal.chunks_deleted,
                "pdfs_deleted": removal.pdfs_deleted,
            }

    # ---- ask ----------------------------------------------------------------------

    @app.post("/api/ask")
    def ask(body: AskRequest):
        with open_library() as library:
            if library.stats()["indexed"] == 0:
                raise HTTPException(409, "No indexed papers yet. Add papers and wait until they are indexed.")
            paper_id = None
            if body.paper_id:
                paper_id = get_stored(library, body.paper_id).paper.arxiv_id
            generator, unavailable_note = None, ""
            if body.generate:
                available, detail = model_status()
                if available:
                    generator = model
                else:
                    unavailable_note = f"No answer was generated: {detail}."
            answer = answer_question(library, body.question, generator, top_k=body.top_k, arxiv_id=paper_id)
            notes = list(answer.notes)
            if unavailable_note and answer.mode == RETRIEVAL_ONLY:
                notes.insert(0, unavailable_note)
            retrieval = answer.retrieval
            cited = {p.rank for p in answer.cited}
            return {
                "mode": answer.mode,
                "question": retrieval.question,
                "keywords": list(retrieval.keywords),
                "missing_keywords": list(retrieval.missing_keywords),
                "enough_evidence": retrieval.enough_evidence,
                "reason": retrieval.reason,
                "answer": answer.text,
                "generator": answer.generator_name,
                "notes": notes,
                "cited": sorted(cited),
                "passages": [
                    {
                        "rank": p.rank,
                        "chunk_id": p.chunk_id,
                        "arxiv_id": p.arxiv_id,
                        "version": p.version,
                        "title": p.title,
                        "page": p.page,
                        "source": p.source,
                        "location": p.location,
                        "citation": p.citation,
                        "link": p.link,
                        "text": p.text,
                        "score": p.score,
                        "matched_keywords": list(p.matched_keywords),
                        "cited": p.rank in cited,
                    }
                    for p in retrieval.passages
                ],
            }

    if WEB_DIR.exists():  # the web page itself (mounted last so /api routes take priority)
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def needs_ingest(stored: StoredPaper) -> bool:
    """False if the paper is already being ingested or its current version's full text is indexed."""
    if stored.ingest_status in IN_PROGRESS:
        return False
    return not (stored.ingest_status == FULL_TEXT and stored.ingested_version == stored.paper.version)


def paper_json(library: Library, stored: StoredPaper) -> dict:
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
        "comment": paper.comment,
        "journal_ref": paper.journal_ref,
        "doi": paper.doi,
        "in_collection": stored.in_collection,
        "status": stored.ingest_status,
        "note": stored.ingest_note,
        "progress": stored.progress,
        "page_count": stored.page_count,
        "chunk_count": library.chunk_count(paper.arxiv_id),
        "ingested_version": stored.ingested_version,
        "newer_version_available": bool(
            stored.ingest_status in INDEXED and stored.ingested_version and stored.ingested_version != paper.version
        ),
        "first_saved_at": stored.first_saved_at,
        "ingested_at": stored.ingested_at,
    }
