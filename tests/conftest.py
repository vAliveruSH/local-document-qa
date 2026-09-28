"""Shared test helpers. Nothing here touches the network."""
from pathlib import Path

import pytest
import requests

from docqa.arxiv_client import ArxivClient, Paper
from docqa.chunking import abstract_chunk, chunk_pages
from docqa.storage import ABSTRACT_ONLY, FULL_TEXT, Library

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Stands in for requests.Session: returns queued responses (or raises queued exceptions)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(*outcomes):
    """An ArxivClient with a fake session and a fake sleep that records waits instead of waiting."""
    session = FakeSession(*outcomes)
    sleeps = []
    client = ArxivClient(session=session, sleep=sleeps.append, clock=lambda: 0.0)
    return client, session, sleeps


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "library.db")
    yield lib
    lib.close()


def paper(arxiv_id, title):
    return Paper(arxiv_id=arxiv_id, version="v1", title=title, authors=(), summary=f"Abstract of {title}.",
                 published="2024-01-01", updated="2024-01-01", abs_url=f"https://arxiv.org/abs/{arxiv_id}v1",
                 pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1")


@pytest.fixture
def collection(tmp_path):
    lib = Library(tmp_path / "library.db")
    a, b, c = paper("2401.00001", "Widgets"), paper("2401.00002", "Gardens"), paper("2401.00003", "Rivers")
    lib.save_papers([a, b, c])
    lib.record_ingest(a.arxiv_id, FULL_TEXT, "2 pages", chunk_pages(a.arxiv_id, [
        "Widgets are small mechanical parts used in clocks.",
        "The optimal gear ratio for widget transmissions is three to one, measured on twelve prototypes.",
    ]), "v1")
    lib.record_ingest(b.arxiv_id, FULL_TEXT, "1 page", chunk_pages(b.arxiv_id, [
        "Tomato plants in gardens need six hours of sunlight and regular watering.",
    ]), "v1")
    lib.record_ingest(c.arxiv_id, ABSTRACT_ONLY, "offline", [
        abstract_chunk(c.arxiv_id, "Rivers", "River sediment transport increases after heavy rainfall."),
    ], "v1")
    yield lib
    lib.close()


__all__ = ["FakeResponse", "FakeSession", "fixture_bytes", "make_client", "requests"]
