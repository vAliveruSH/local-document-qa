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

NOT_INGESTED = "not_ingested"

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
    ingested_at      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Library:
    def __init__(self, db_path: Path | str):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

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

    def list_papers(self) -> list[StoredPaper]:
        rows = self.conn.execute("SELECT * FROM papers ORDER BY first_saved_at, arxiv_id").fetchall()
        return [_row_to_stored(row) for row in rows]

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
    )
