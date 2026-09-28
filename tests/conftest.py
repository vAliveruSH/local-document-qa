"""Shared test helpers. Nothing here touches the network."""
from pathlib import Path

import pytest
import requests

from docqa.arxiv_client import ArxivClient
from docqa.storage import Library

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


__all__ = ["FakeResponse", "FakeSession", "fixture_bytes", "make_client", "requests"]
