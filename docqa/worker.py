"""Background ingestion for the web interface.

Papers are ingested one at a time on a separate thread, so the web page stays responsive
and arXiv never gets more than one download at once. Progress is written to the database
by ingest.py, and the web page reads it from there.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from .arxiv_client import ArxivClient
from .storage import FAILED, Library
from .workflow import ingest_by_id


class IngestWorker:
    def __init__(self, db_path: Path, pdf_dir: Path, client: ArxivClient):
        self.db_path = db_path
        self.pdf_dir = pdf_dir
        self.client = client
        self._queue: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="ingest-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, arxiv_id: str, force: bool = False) -> None:
        self._queue.put((arxiv_id, force))

    def wait_until_idle(self) -> None:
        """Block until every queued paper has been processed (used by tests)."""
        self._queue.join()

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=10)

    def _run(self) -> None:
        library = Library(self.db_path)  # this thread's own database connection
        try:
            while (item := self._queue.get()) is not None:
                arxiv_id, force = item
                try:
                    ingest_by_id(self.client, library, arxiv_id, self.pdf_dir, force=force)
                except Exception as exc:  # never let one paper stop the worker
                    library.set_state(arxiv_id, FAILED, f"unexpected error: {exc}")
                finally:
                    self._queue.task_done()
            self._queue.task_done()
        finally:
            library.close()
