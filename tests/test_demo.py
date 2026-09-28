"""Tests for the public read-only demo (docqa/demo.py).

The key promise: whatever a client sends, the demo can't search arXiv, ingest, delete, write
to its database, call a language model, or reach the network. These tests call the forbidden
routes directly (not through the page) and check the database file is byte-for-byte unchanged.
"""
import hashlib
import json
import socket
import sqlite3
import stat
import os

import pytest
import requests
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import docqa.arxiv_client
import docqa.demo as demo
import docqa.local_llm
import docqa.worker
from conftest import build_demo_folder, unlock_folder
from docqa.demo import RateLimiter, bundle_database, create_demo_app
from docqa.storage import Library

QUESTION = "What gear ratio is optimal for widget transmissions?"


def fingerprint(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


@pytest.fixture
def no_outside_world(monkeypatch):
    """Any attempt to open a network connection, build an arXiv client, a model client, or a worker fails."""
    calls = []

    def forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"the demo must never use {name}")
        return fail

    # Outbound connections need a host-name lookup or create_connection (requests/urllib3 use both).
    # socket.connect itself is left alone: the test client's event loop uses a local socket pair.
    monkeypatch.setattr(socket, "getaddrinfo", forbidden("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))
    monkeypatch.setattr(requests.Session, "request", forbidden("requests"))
    monkeypatch.setattr(docqa.arxiv_client.ArxivClient, "__init__", forbidden("ArxivClient"))
    monkeypatch.setattr(docqa.local_llm.OllamaGenerator, "__init__", forbidden("OllamaGenerator"))
    monkeypatch.setattr(docqa.worker.IngestWorker, "__init__", forbidden("IngestWorker"))
    return calls


@pytest.fixture
def demo_folder(tmp_path):
    folder = build_demo_folder(tmp_path / "demo")
    before = fingerprint(folder)
    yield folder
    # Every test that used the demo must leave its files byte-for-byte unchanged, with no new files.
    assert fingerprint(folder) == before
    unlock_folder(folder)


@pytest.fixture
def api(demo_folder, no_outside_world):
    return TestClient(create_demo_app(demo_folder=demo_folder, repo_url="https://example.org/repo"))


def test_the_outside_world_guard_really_blocks(no_outside_world):
    # Proves the guard used by the tests below is not a no-op.
    with pytest.raises(AssertionError):
        requests.get("https://export.arxiv.org/api/query")
    with pytest.raises(AssertionError):
        docqa.arxiv_client.ArxivClient()
    with pytest.raises(AssertionError):
        docqa.local_llm.OllamaGenerator()
    with pytest.raises(AssertionError):
        socket.create_connection(("arxiv.org", 443))
    assert len(no_outside_world) == 4


# ---- the API surface --------------------------------------------------------------------------

def test_only_read_routes_exist():
    app = create_demo_app()
    routes = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert routes == {
        ("/api/status", "GET"),
        ("/api/papers", "GET"),
        ("/api/papers/{arxiv_id:path}", "GET"),
        ("/api/ask", "POST"),
    }


FORBIDDEN = [
    ("GET", "/api/search?q=attention", None),
    ("POST", "/api/collection", {"ids": ["2401.00001"]}),
    ("POST", "/api/papers/2401.00001/ingest", {"force": True}),
    ("DELETE", "/api/papers/2401.00001", None),
    ("PUT", "/api/papers/2401.00001", {"title": "x"}),
    ("PATCH", "/api/papers/2401.00001", {"title": "x"}),
    ("POST", "/api/papers/2401.00001", None),
    ("POST", "/api/status", None),
    ("DELETE", "/api/papers", None),
    ("GET", "/docs", None),
    ("GET", "/redoc", None),
    ("GET", "/openapi.json", None),
]


@pytest.mark.parametrize("method, path, body", FORBIDDEN)
def test_forbidden_actions_cannot_be_called_directly(api, no_outside_world, method, path, body):
    response = api.request(method, path, json=body)
    assert response.status_code in (404, 405), response.text
    assert no_outside_world == []
    # The library still holds exactly what it did.
    assert [p["arxiv_id"] for p in api.get("/api/papers").json()["papers"]] == ["2401.00001", "2401.00002"]


# ---- what the demo does -----------------------------------------------------------------------

def test_status_reports_demo_mode(api):
    body = api.get("/api/status").json()
    assert body["mode"] == "demo"
    assert body["demo"]["available"] is True and body["demo"]["repo_url"] == "https://example.org/repo"
    assert body["arxiv"]["state"] == "disabled"
    assert body["model"]["available"] is False
    assert body["stats"]["papers"] == 2 and body["stats"]["indexed"] == 2


def test_papers_carry_licence_and_no_local_paths(api):
    papers = api.get("/api/papers").json()["papers"]
    assert [p["license"]["name"] for p in papers] == ["CC BY 4.0", "CC BY 4.0"]
    assert papers[0]["license"]["evidence"] == "https://arxiv.org/abs/2401.00001v1"
    assert "Text extracted from the PDF" in papers[0]["license"]["changes"]
    assert "pdf_path" not in papers[0]


def test_ask_returns_licensed_passages_and_never_generates(api, no_outside_world):
    body = api.post("/api/ask", json={"question": QUESTION}).json()
    assert body["mode"] == "retrieval_only" and body["answer"] == ""
    assert body["notes"][0].startswith("Public demo: no answer model runs here")
    top = body["passages"][0]
    assert top["arxiv_id"] == "2401.00001" and top["page"] == 2
    assert top["license"]["name"] == "CC BY 4.0"
    assert no_outside_world == []


def test_ask_not_enough_evidence(api):
    body = api.post("/api/ask", json={"question": "What is the capital of France?"}).json()
    assert body["mode"] == "insufficient_evidence" and body["passages"] == []


def test_ask_can_be_scoped_to_one_demo_paper(api):
    body = api.post("/api/ask", json={"question": "widgets tomato sunlight", "paper_id": "2401.00002"}).json()
    assert {p["arxiv_id"] for p in body["passages"]} == {"2401.00002"}
    assert api.post("/api/ask", json={"question": "x", "paper_id": "1706.03762"}).status_code == 404
    assert api.post("/api/ask", json={"question": "x", "paper_id": "not an id"}).status_code == 400
    assert api.get("/api/papers/1706.03762").status_code == 404


@pytest.mark.parametrize("body", [
    {"question": QUESTION, "generate": True},          # asking for a model is refused, not ignored
    {"question": QUESTION, "ollama_host": "http://evil"},
    {"question": "x" * 501},
    {"question": ""},
    {"question": QUESTION, "top_k": 11},
    {"question": QUESTION, "top_k": 0},
])
def test_ask_rejects_bad_or_unknown_input(api, body):
    assert api.post("/api/ask", json=body).status_code == 422


def test_rate_limit_per_visitor(api):
    for _ in range(demo.ASKS_PER_MINUTE_PER_VISITOR):
        assert api.post("/api/ask", json={"question": QUESTION}).status_code == 200
    blocked = api.post("/api/ask", json={"question": QUESTION})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "wait a minute" in blocked.json()["detail"]


def test_rate_limiter_window_and_instance_cap():
    now = [0.0]
    limiter = RateLimiter(per_visitor=2, per_instance=3, window=60, clock=lambda: now[0])
    assert limiter.retry_after("a") == 0 and limiter.retry_after("a") == 0
    assert limiter.retry_after("a") > 0          # visitor limit
    assert limiter.retry_after("b") == 0          # third request overall
    assert limiter.retry_after("c") > 0           # instance cap
    now[0] = 61
    assert limiter.retry_after("a") == 0          # window has passed


def test_forwarded_ip_is_trusted_only_on_vercel(monkeypatch):
    class FakeRequest:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
        client = type("C", (), {"host": "10.1.1.1"})()

    monkeypatch.delenv("VERCEL", raising=False)
    assert demo.visitor_key(FakeRequest()) == "10.1.1.1"  # elsewhere the header could be faked
    monkeypatch.setenv("VERCEL", "1")
    assert demo.visitor_key(FakeRequest()) == "203.0.113.9"


def test_security_headers_and_script_hash(api):
    response = api.get("/")
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'sha256-" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert api.get("/api/status").headers["Cache-Control"] == "no-store"


def test_responses_never_contain_server_paths(api, demo_folder):
    texts = [
        api.get("/api/status").text,
        api.get("/api/papers").text,
        api.get("/api/papers/2401.00001").text,
        api.post("/api/ask", json={"question": QUESTION}).text,
    ]
    for text in texts:
        assert str(demo_folder) not in text and demo_folder.as_posix() not in text
        assert "secret" not in text and "library.db" not in text


def test_unexpected_errors_are_generic(demo_folder, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"failure reading {demo_folder}/library.db")

    monkeypatch.setattr(demo, "answer_question", boom)
    client = TestClient(create_demo_app(demo_folder=demo_folder), raise_server_exceptions=False)
    response = client.post("/api/ask", json={"question": QUESTION})
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong on the server."}


# ---- refusing to publish a corpus that isn't safe -----------------------------------------------

def test_missing_corpus_is_reported_not_crashing(tmp_path):
    client = TestClient(create_demo_app(demo_folder=tmp_path / "nothing-here"))
    status = client.get("/api/status").json()
    assert status["demo"]["available"] is False and "not been built" in status["demo"]["problem"]
    assert client.get("/api/papers").status_code == 503
    assert client.post("/api/ask", json={"question": QUESTION}).status_code == 503


def rewrite_manifest(folder, papers):
    manifest = folder / "corpus.json"
    if manifest.exists():
        os.chmod(manifest, stat.S_IREAD | stat.S_IWRITE)
    manifest.write_text(json.dumps({"papers": papers}), encoding="utf-8")


CC_BY = "https://creativecommons.org/licenses/by/4.0/"


@pytest.mark.parametrize("papers, problem", [
    ([{"arxiv_id": "2401.00001", "version": "v1", "license_url": CC_BY}], "list different papers"),
    ([{"arxiv_id": "2401.00001", "version": "v1", "license_url": CC_BY},
      {"arxiv_id": "2401.00002", "version": "v1", "license_url": "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"}],
     "not allowed"),
    ([{"arxiv_id": "2401.00001", "version": "v1", "license_url": CC_BY},
      {"arxiv_id": "2401.00002", "version": "v2", "license_url": CC_BY}], "without a matching licence"),
    ([{"arxiv_id": "2401.00001"}], "invalid"),
])
def test_corpus_without_matching_licences_is_refused(tmp_path, papers, problem):
    folder = build_demo_folder(tmp_path / "demo")
    rewrite_manifest(folder, papers)
    client = TestClient(create_demo_app(demo_folder=folder))
    assert problem in client.get("/api/status").json()["demo"]["problem"]
    assert client.post("/api/ask", json={"question": QUESTION}).status_code == 503
    unlock_folder(folder)


def test_unbundled_database_with_local_paths_is_refused(tmp_path):
    folder = tmp_path / "demo"
    folder.mkdir()
    with Library(folder / "library.db") as lib:  # a normal library, not passed through bundle_database
        from conftest import paper
        lib.save_papers([paper("2401.00001", "Widgets")])
        lib.add_to_collection(["2401.00001"])
        lib.record_ingest("2401.00001", "full_text", "1 page", [], "v1", "C:/Users/me/pdfs/x.pdf", 1)
    rewrite_manifest(folder, [{"arxiv_id": "2401.00001", "version": "v1", "license_url": CC_BY}])
    client = TestClient(create_demo_app(demo_folder=folder))
    assert "not built for publishing" in client.get("/api/status").json()["demo"]["problem"]


# ---- the bundled database itself ----------------------------------------------------------------

def test_bundle_is_a_single_clean_file(demo_folder):
    files = sorted(p.name for p in demo_folder.iterdir())
    assert files == ["corpus.json", "library.db"]  # no -wal / -shm / -journal files
    conn = sqlite3.connect(f"{(demo_folder / 'library.db').as_uri()}?mode=ro", uri=True)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2        # cached search result dropped
    assert conn.execute("SELECT COUNT(*) FROM papers WHERE pdf_path != ''").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    conn.close()


def test_bundling_merges_wal_data(tmp_path):
    source = tmp_path / "source.db"
    lib = Library(source)  # WAL mode; keep it open so recent writes may still sit in the -wal file
    from conftest import paper
    lib.save_papers([paper("2401.00001", "Widgets")])
    lib.add_to_collection(["2401.00001"])
    bundle_database(source, tmp_path / "out" / "library.db")
    lib.close()
    with Library.open_read_only(tmp_path / "out" / "library.db") as out:
        assert [s.paper.arxiv_id for s in out.list_papers()] == ["2401.00001"]


FIRST_PAGE = ("Widgets for Clocks Ada Smith Bo Chen {asmith, bchen}@uni.edu Cy Diaz cy.diaz+work@lab.example.org "
              "The optimal gear ratio for widget transmissions is three to one.")


def test_bundling_removes_author_emails_from_passages_and_index(tmp_path):
    from conftest import paper
    from docqa.chunking import chunk_pages

    import dataclasses

    source = tmp_path / "source.db"
    with Library(source) as lib:
        lib.save_papers([dataclasses.replace(paper("2401.00001", "Widgets for Clocks"), authors=("Ada Smith", "Bo Chen"))])
        lib.add_to_collection(["2401.00001"])
        lib.record_ingest("2401.00001", "full_text", "1 page", chunk_pages("2401.00001", [FIRST_PAGE]), "v1", "", 1)
    bundle_database(source, tmp_path / "out" / "library.db")

    with Library.open_read_only(tmp_path / "out" / "library.db") as out:
        (chunk,) = out.get_chunks("2401.00001")
        assert "@" not in chunk.text and "asmith" not in chunk.text and "cy.diaz" not in chunk.text
        assert chunk.text.count(demo.EMAIL_REMOVED) == 2
        assert "Ada Smith" in chunk.text and "gear ratio" in chunk.text  # names and content are kept
        stored = out.get_paper("2401.00001").paper  # title and author names in the metadata are kept
        assert stored.title == "Widgets for Clocks" and stored.authors == ("Ada Smith", "Bo Chen")
        for word in ("asmith", "bchen", "uni", "lab", "example"):  # the search index no longer finds them
            assert out.search_chunks(f'"{word}"', 5) == []
        assert len(out.search_chunks('"gear"', 5)) == 1


def test_database_with_email_addresses_is_refused(tmp_path):
    from conftest import paper
    from docqa.chunking import chunk_pages

    folder = tmp_path / "demo"
    folder.mkdir()
    with Library(folder / "library.db") as lib:  # emails left in, pdf_path already empty
        lib.save_papers([paper("2401.00001", "Widgets for Clocks")])
        lib.add_to_collection(["2401.00001"])
        lib.record_ingest("2401.00001", "full_text", "1 page", chunk_pages("2401.00001", [FIRST_PAGE]), "v1", "", 1)
    rewrite_manifest(folder, [{"arxiv_id": "2401.00001", "version": "v1", "license_url": CC_BY}])
    client = TestClient(create_demo_app(demo_folder=folder))
    assert "not built for publishing" in client.get("/api/status").json()["demo"]["problem"]


WRITES = [
    "INSERT INTO meta VALUES ('k', 'v')",
    "UPDATE papers SET title = 'x'",
    "DELETE FROM chunks",
    "DELETE FROM chunks_fts",
    "CREATE TABLE t (x)",
    "DROP TABLE meta",
    "ALTER TABLE papers ADD COLUMN x",
    "PRAGMA journal_mode = WAL",
    "BEGIN",
]


@pytest.mark.parametrize("sql", WRITES)
def test_read_only_library_blocks_every_write(demo_folder, sql):
    with Library.open_read_only(demo_folder / "library.db") as lib:
        with pytest.raises(sqlite3.DatabaseError):
            lib.conn.execute(sql)


def test_read_only_library_blocks_write_methods(demo_folder):
    with Library.open_read_only(demo_folder / "library.db") as lib:
        for call in (lambda: lib.set_meta("k", "v"), lambda: lib.delete_paper("2401.00001"),
                     lambda: lib.add_to_collection(["2401.00001"]), lambda: lib.mark_interrupted()):
            with pytest.raises(sqlite3.DatabaseError):
                call()
        assert len(lib.list_papers()) == 2


def test_read_only_open_of_missing_file_hides_the_path(tmp_path):
    with pytest.raises(FileNotFoundError) as info:
        Library.open_read_only(tmp_path / "secret-folder" / "library.db")
    assert "secret-folder" not in str(info.value)
