"""Local storage in a single SQLite file (data/library.db by default).

SQLite ships with Python, needs no server, and survives restarts. The arXiv ID is the
primary key of the papers table, so saving the same paper twice updates it instead of
creating a duplicate.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .arxiv_client import Paper
from .chunking import Chunk

# Ingest statuses stored for each paper.
NOT_INGESTED = "not_ingested"  # metadata only
QUEUED = "queued"  # waiting for the background worker
DOWNLOADING = "downloading"
PROCESSING = "processing"  # extracting text and indexing
FULL_TEXT = "full_text"  # PDF text was extracted and indexed
ABSTRACT_ONLY = "abstract_only"  # only the abstract is indexed (PDF missing or unreadable)
FAILED = "failed"  # ingestion stopped with an error; ingest_note says why
IN_PROGRESS = (QUEUED, DOWNLOADING, PROCESSING)
INDEXED = (FULL_TEXT, ABSTRACT_ONLY)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id         TEXT PRIMARY KEY,
    version          TEXT NOT NULL,
    title            TEXT NOT NULL,
    authors          TEXT NOT NULL,   -- JSON list of names
    summary          TEXT NOT NULL,
    published        TEXT NOT NULL,
    updated          TEXT NOT NULL,
    abs_url          TEXT NOT NULL,
    pdf_url          TEXT NOT NULL,
    primary_category TEXT NOT NULL,
    categories       TEXT NOT NULL,   -- JSON list
    comment          TEXT NOT NULL,
    journal_ref      TEXT NOT NULL,
    doi              TEXT NOT NULL,
    first_saved_at   TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    ingest_status    TEXT NOT NULL DEFAULT 'not_ingested',
    ingest_note      TEXT NOT NULL DEFAULT '',
    ingested_version TEXT NOT NULL DEFAULT '',
    pdf_path         TEXT NOT NULL DEFAULT '',
    page_count       INTEGER,
    ingested_at      TEXT NOT NULL DEFAULT '',
    in_collection    INTEGER NOT NULL DEFAULT 0,  -- 1 once the user adds the paper; 0 = only seen in a search
    progress         INTEGER                       -- download percentage while downloading
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    arxiv_id    TEXT NOT NULL REFERENCES papers(arxiv_id),
    chunk_index INTEGER NOT NULL,
    page        INTEGER,          -- 1-based PDF page; NULL for abstract text
    source      TEXT NOT NULL,    -- 'pdf' or 'abstract'
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_paper ON chunks(arxiv_id);
-- Full-text search index over chunk text. 'porter' stemming lets "transformers" match "transformer".
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);
"""

METADATA_COLUMNS = (
    "version", "title", "authors", "summary", "published", "updated", "abs_url", "pdf_url",
    "primary_category", "categories", "comment", "journal_ref", "doi",
)


@dataclass
class SaveReport:
    new: list[str] = field(default_factory=list)
    already_saved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StoredPaper:
    paper: Paper
    first_saved_at: str
    last_seen_at: str
    ingest_status: str
    ingest_note: str
    ingested_version: str
    pdf_path: str
    page_count: int | None
    ingested_at: str
    in_collection: bool = False
    progress: int | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Library:
    def __init__(self, db_path: Path | str):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # timeout: wait for another connection's write instead of failing (the web server and its
        # background worker use separate connections to the same file).
        self.conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(db_path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release, so older library files keep working."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(papers)")}
        with self.conn:
            if "in_collection" not in columns:
                self.conn.execute("ALTER TABLE papers ADD COLUMN in_collection INTEGER NOT NULL DEFAULT 0")
                # Before this column existed, a paper was "added" exactly when it had been ingested.
                self.conn.execute("UPDATE papers SET in_collection = 1 WHERE ingest_status != ?", (NOT_INGESTED,))
            if "progress" not in columns:
                self.conn.execute("ALTER TABLE papers ADD COLUMN progress INTEGER")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- papers -------------------------------------------------------------

    def save_papers(self, papers: list[Paper]) -> SaveReport:
        """Insert new papers and refresh metadata of known ones, all in one transaction."""
        report = SaveReport()
        timestamp = now_iso()
        with self.conn:
            for paper in papers:
                values = _metadata_values(paper)
                if self._exists(paper.arxiv_id):
                    assignments = ", ".join(f"{col} = ?" for col in METADATA_COLUMNS)
                    self.conn.execute(
                        f"UPDATE papers SET {assignments}, last_seen_at = ? WHERE arxiv_id = ?",
                        (*values, timestamp, paper.arxiv_id),
                    )
                    report.already_saved.append(paper.arxiv_id)
                else:
                    columns = ", ".join(("arxiv_id", *METADATA_COLUMNS, "first_saved_at", "last_seen_at"))
                    placeholders = ", ".join("?" * (len(METADATA_COLUMNS) + 3))
                    self.conn.execute(
                        f"INSERT INTO papers ({columns}) VALUES ({placeholders})",
                        (paper.arxiv_id, *values, timestamp, timestamp),
                    )
                    report.new.append(paper.arxiv_id)
        return report

    def get_paper(self, arxiv_id: str) -> StoredPaper | None:
        row = self.conn.execute("SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        return _row_to_stored(row) if row else None

    def list_papers(self, collection_only: bool = False) -> list[StoredPaper]:
        where = "WHERE in_collection = 1" if collection_only else ""
        rows = self.conn.execute(f"SELECT * FROM papers {where} ORDER BY first_saved_at, arxiv_id").fetchall()
        return [_row_to_stored(row) for row in rows]

    def add_to_collection(self, arxiv_ids: list[str]) -> None:
        with self.conn:
            self.conn.executemany("UPDATE papers SET in_collection = 1 WHERE arxiv_id = ?", [(i,) for i in arxiv_ids])

    def set_state(self, arxiv_id: str, status: str, note: str = "", progress: int | None = None) -> None:
        """Record where a paper is in ingestion (queued, downloading, processing, failed)."""
        with self.conn:
            self.conn.execute(
                "UPDATE papers SET ingest_status = ?, ingest_note = ?, progress = ? WHERE arxiv_id = ?",
                (status, note, progress, arxiv_id),
            )

    def set_progress(self, arxiv_id: str, progress: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE papers SET progress = ? WHERE arxiv_id = ?", (progress, arxiv_id))

    def mark_interrupted(self) -> int:
        """Papers left mid-ingestion when the app stopped become 'failed' so they can be retried."""
        placeholders = ", ".join("?" * len(IN_PROGRESS))
        with self.conn:
            cursor = self.conn.execute(
                f"""UPDATE papers SET ingest_status = ?, progress = NULL,
                    ingest_note = 'interrupted: the app stopped before ingestion finished'
                    WHERE ingest_status IN ({placeholders})""",
                (FAILED, *IN_PROGRESS),
            )
        return cursor.rowcount

    def delete_paper(self, arxiv_id: str) -> int:
        """Delete a paper, its chunks and their index entries. Returns how many chunks were removed."""
        chunks = self.chunk_count(arxiv_id)
        with self.conn:
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE arxiv_id = ?)",
                (arxiv_id,),
            )
            self.conn.execute("DELETE FROM chunks WHERE arxiv_id = ?", (arxiv_id,))
            self.conn.execute("DELETE FROM papers WHERE arxiv_id = ?", (arxiv_id,))
        return chunks

    def stats(self) -> dict:
        """Counts over the user's collection (papers they added), for summaries and badges."""
        rows = self.conn.execute(
            "SELECT ingest_status, COUNT(*) AS n FROM papers WHERE in_collection = 1 GROUP BY ingest_status"
        ).fetchall()
        by_status = {row["ingest_status"]: row["n"] for row in rows}
        return {
            "papers": sum(by_status.values()),
            "indexed": sum(by_status.get(s, 0) for s in INDEXED),
            "full_text": by_status.get(FULL_TEXT, 0),
            "abstract_only": by_status.get(ABSTRACT_ONLY, 0),
            "in_progress": sum(by_status.get(s, 0) for s in IN_PROGRESS),
            "failed": by_status.get(FAILED, 0),
            "metadata_only": by_status.get(NOT_INGESTED, 0),
            "chunks": self.chunk_count(),
            "cached_search_results": self.conn.execute(
                "SELECT COUNT(*) FROM papers WHERE in_collection = 0"
            ).fetchone()[0],
        }

    def record_ingest(
        self,
        arxiv_id: str,
        status: str,
        note: str,
        chunks: list[Chunk],
        version: str,
        pdf_path: str = "",
        page_count: int | None = None,
    ) -> None:
        """Replace a paper's chunks (and their search-index entries) and store its ingest status.

        Everything happens in one transaction: re-ingesting never leaves duplicate or
        half-written chunks behind.
        """
        with self.conn:
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE arxiv_id = ?)",
                (arxiv_id,),
            )
            self.conn.execute("DELETE FROM chunks WHERE arxiv_id = ?", (arxiv_id,))
            self.conn.executemany(
                "INSERT INTO chunks (chunk_id, arxiv_id, chunk_index, page, source, text) VALUES (?, ?, ?, ?, ?, ?)",
                [(c.chunk_id, c.arxiv_id, c.chunk_index, c.page, c.source, c.text) for c in chunks],
            )
            self.conn.executemany(
                "INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)", [(c.chunk_id, c.text) for c in chunks]
            )
            self.conn.execute(
                """UPDATE papers SET ingest_status = ?, ingest_note = ?, ingested_version = ?,
                   pdf_path = ?, page_count = ?, ingested_at = ?, progress = NULL WHERE arxiv_id = ?""",
                (status, note, version, pdf_path, page_count, now_iso(), arxiv_id),
            )

    def get_chunks(self, arxiv_id: str) -> list[Chunk]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE arxiv_id = ? ORDER BY chunk_index", (arxiv_id,)
        ).fetchall()
        return [Chunk(r["chunk_id"], r["arxiv_id"], r["chunk_index"], r["page"], r["source"], r["text"]) for r in rows]

    def chunk_count(self, arxiv_id: str | None = None) -> int:
        if arxiv_id is None:
            return self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM chunks WHERE arxiv_id = ?", (arxiv_id,)).fetchone()[0]

    # ---- full-text search ------------------------------------------------------

    def search_chunks(self, fts_query: str, limit: int, arxiv_id: str | None = None) -> list[sqlite3.Row]:
        """Best-matching chunks for an FTS5 query, ranked by BM25 (lower bm25() = better match)."""
        paper_filter = "AND c.arxiv_id = ?" if arxiv_id else ""
        params = (fts_query, arxiv_id, limit) if arxiv_id else (fts_query, limit)
        return self.conn.execute(
            f"""SELECT c.chunk_id, c.arxiv_id, c.page, c.source, c.text,
                       p.title, p.version, p.abs_url, p.pdf_url, p.ingest_status,
                       bm25(chunks_fts) AS bm25
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                JOIN papers p ON p.arxiv_id = c.arxiv_id
                WHERE chunks_fts MATCH ? {paper_filter}
                ORDER BY bm25 LIMIT ?""",
            params,
        ).fetchall()

    def chunks_containing(self, fts_query: str, chunk_ids: list[str]) -> set[str]:
        """Which of the given chunks match an FTS5 query (used to see which question words a passage has)."""
        if not chunk_ids:
            return set()
        placeholders = ", ".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? AND chunk_id IN ({placeholders})",
            (fts_query, *chunk_ids),
        ).fetchall()
        return {row["chunk_id"] for row in rows}

    def _exists(self, arxiv_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone() is not None

    # ---- small key/value facts, e.g. when the last search happened -----------

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def _metadata_values(paper: Paper) -> tuple:
    return (
        paper.version, paper.title, json.dumps(list(paper.authors)), paper.summary, paper.published,
        paper.updated, paper.abs_url, paper.pdf_url, paper.primary_category,
        json.dumps(list(paper.categories)), paper.comment, paper.journal_ref, paper.doi,
    )


def _row_to_stored(row: sqlite3.Row) -> StoredPaper:
    paper = Paper(
        arxiv_id=row["arxiv_id"],
        version=row["version"],
        title=row["title"],
        authors=tuple(json.loads(row["authors"])),
        summary=row["summary"],
        published=row["published"],
        updated=row["updated"],
        abs_url=row["abs_url"],
        pdf_url=row["pdf_url"],
        primary_category=row["primary_category"],
        categories=tuple(json.loads(row["categories"])),
        comment=row["comment"],
        journal_ref=row["journal_ref"],
        doi=row["doi"],
    )
    return StoredPaper(
        paper=paper,
        first_saved_at=row["first_saved_at"],
        last_seen_at=row["last_seen_at"],
        ingest_status=row["ingest_status"],
        ingest_note=row["ingest_note"],
        ingested_version=row["ingested_version"],
        pdf_path=row["pdf_path"],
        page_count=row["page_count"],
        ingested_at=row["ingested_at"],
        in_collection=bool(row["in_collection"]),
        progress=row["progress"],
    )
