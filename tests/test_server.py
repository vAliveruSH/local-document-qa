"""End-to-end tests of the web API: real server code, real SQLite, real background worker.

Only the network is faked: arXiv responses come from saved fixtures and PDFs are generated.
"""
import pytest
import requests
from fastapi.testclient import TestClient

from conftest import FakeResponse, fixture_bytes, make_client
from docqa.local_llm import GeneratorError
from docqa.server import create_app
from docqa.storage import FAILED, Library
from pdf_maker import make_pdf

SEARCH_XML = fixture_bytes("arxiv_search.xml")  # papers 2209.15001 and 2605.26355
PDF = make_pdf([
    "Dilated neighborhood attention expands receptive fields exponentially at no additional cost. " * 12,
    "DiNAT reaches 58.5 PQ on COCO panoptic segmentation with the large model variant. " * 12,
])


class FakeModel:
    name = "fake-model via Ollama"

    def __init__(self, available=False, reply="", error=None):
        self.available, self.reply, self.error = available, reply, error

    def check(self):
        return (True, self.name) if self.available else (False, "Ollama was not found at http://localhost:11434")

    def generate(self, prompt):
        if self.error:
            raise GeneratorError(self.error)
        return self.reply


def start(tmp_path, *responses, model=None):
    client, session, _ = make_client(*responses)
    app = create_app(data_dir=tmp_path, client=client, model=model or FakeModel())
    return TestClient(app), app, session


def search_and_add(api, app, ids=("2209.15001",)):
    assert api.get("/api/search", params={"q": "attention transformer"}).status_code == 200
    response = api.post("/api/collection", json={"ids": list(ids)})
    app.state.worker.wait_until_idle()
    return response


# ---- status and discover ----------------------------------------------------------

def test_status_of_a_new_library(tmp_path):
    api, _, _ = start(tmp_path)
    with api:
        body = api.get("/api/status").json()
    assert body["stats"]["papers"] == 0 and body["stats"]["indexed"] == 0
    assert body["arxiv"]["state"] == "unknown"
    assert body["model"] == {"available": False, "name": "fake-model via Ollama",
                             "detail": "Ollama was not found at http://localhost:11434"}
    assert body["index"]["engine"] == "SQLite FTS5 · BM25"


def test_search_returns_results_without_adding_them_to_the_collection(tmp_path):
    api, _, session = start(tmp_path, FakeResponse(200, SEARCH_XML))
    with api:
        body = api.get("/api/search", params={"q": "attention transformer", "sort": "recent", "max": 2}).json()
        stats = api.get("/api/status").json()["stats"]
        papers = api.get("/api/papers").json()["papers"]

    assert [r["arxiv_id"] for r in body["results"]] == ["2209.15001", "2605.26355"]
    first = body["results"][0]
    assert first["title"] == "Dilated Neighborhood Attention Transformer"
    assert first["authors"] == ["Ali Hassani", "Humphrey Shi"]
    assert first["in_collection"] is False and first["status"] == "not_ingested"
    assert body["total_results"] == 18757
    assert session.calls[0]["params"]["sortBy"] == "submittedDate"
    assert stats["papers"] == 0 and stats["cached_search_results"] == 2
    assert papers == []


def test_search_failure_reports_error_and_changes_nothing(tmp_path):
    api, _, _ = start(tmp_path, *[requests.ConnectionError("offline")] * 4)
    with api:
        response = api.get("/api/search", params={"q": "attention"})
        status = api.get("/api/status").json()
    assert response.status_code == 502
    assert "after 4 attempts" in response.json()["detail"]
    assert status["arxiv"]["state"] == "error"
    assert status["stats"]["cached_search_results"] == 0


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "x", "sort": "random"}, {"q": "x", "max": 99}])
def test_invalid_search_input_is_rejected(tmp_path, params):
    api, _, session = start(tmp_path)
    with api:
        assert api.get("/api/search", params=params).status_code == 422
    assert session.calls == []


# ---- collection and ingestion -------------------------------------------------------

def test_adding_a_paper_ingests_it_in_the_background(tmp_path):
    api, app, session = start(tmp_path, FakeResponse(200, SEARCH_XML),
                              FakeResponse(200, PDF, headers={"Content-Length": str(len(PDF))}))
    with api:
        added = search_and_add(api, app).json()
        listing = api.get("/api/papers").json()

    assert added == {"queued": ["2209.15001"], "already_in_collection": [], "problems": []}
    assert session.calls[1]["url"] == "https://arxiv.org/pdf/2209.15001v3" and session.calls[1]["stream"]
    paper = listing["papers"][0]
    assert paper["status"] == "full_text" and paper["page_count"] == 2 and paper["chunk_count"] > 0
    assert paper["progress"] is None
    assert listing["stats"]["indexed"] == 1 and listing["stats"]["papers"] == 1
    assert (tmp_path / "pdfs" / "2209.15001v3.pdf").exists()


def test_adding_again_does_not_download_again(tmp_path):
    api, app, session = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF))
    with api:
        search_and_add(api, app)
        again = api.post("/api/collection", json={"ids": ["2209.15001"]}).json()
    assert again["queued"] == [] and again["already_in_collection"] == ["2209.15001"]
    assert len(session.calls) == 2


def test_unavailable_pdf_becomes_abstract_only_with_reason(tmp_path):
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(404))
    with api:
        search_and_add(api, app)
        paper = api.get("/api/papers/2209.15001").json()
    assert paper["status"] == "abstract_only"
    assert "full text unavailable" in paper["note"] and "HTTP 404" in paper["note"]
    assert paper["chunk_count"] == 1


def test_invalid_and_unknown_ids_are_reported(tmp_path):
    api, _, _ = start(tmp_path, FakeResponse(200, fixture_bytes("arxiv_id_not_found.xml")))
    with api:
        body = api.post("/api/collection", json={"ids": ["not an id", "2001.99999"]}).json()
    assert {p["id"]: p["status"] for p in body["problems"]} == {"not an id": "invalid_id", "2001.99999": "not_found"}
    assert body["queued"] == []


def test_retry_ingestion_of_a_failed_paper(tmp_path):
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF))
    with api:
        api.get("/api/search", params={"q": "attention"})
        with Library(tmp_path / "library.db") as lib:
            lib.add_to_collection(["2209.15001"])
            lib.set_state("2209.15001", FAILED, "could not save files: disk full")
        queued = api.post("/api/papers/2209.15001/ingest", json={}).json()
        app.state.worker.wait_until_idle()
        paper = api.get("/api/papers/2209.15001").json()
    assert queued["status"] == "queued"
    assert paper["status"] == "full_text"


def test_retry_is_refused_while_ingesting(tmp_path):
    api, _, _ = start(tmp_path, FakeResponse(200, SEARCH_XML))
    with api:
        api.get("/api/search", params={"q": "attention"})
        with Library(tmp_path / "library.db") as lib:
            lib.set_state("2209.15001", "downloading", "Downloading", progress=30)
        assert api.post("/api/papers/2209.15001/ingest", json={}).status_code == 409
        assert api.delete("/api/papers/2209.15001").status_code == 409


def test_remove_paper_deletes_everything(tmp_path):
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF))
    with api:
        search_and_add(api, app)
        chunks = api.get("/api/papers/2209.15001").json()["chunk_count"]
        removed = api.delete("/api/papers/2209.15001").json()
        missing = api.get("/api/papers/2209.15001")
        stats = api.get("/api/status").json()["stats"]
    assert removed == {"arxiv_id": "2209.15001", "title": "Dilated Neighborhood Attention Transformer",
                       "chunks_deleted": chunks, "pdfs_deleted": 1}
    assert missing.status_code == 404
    assert stats["chunks"] == 0 and stats["papers"] == 0
    assert not (tmp_path / "pdfs" / "2209.15001v3.pdf").exists()


def test_interrupted_ingestion_is_marked_failed_on_startup(tmp_path):
    api, _, _ = start(tmp_path, FakeResponse(200, SEARCH_XML))
    with api:
        api.get("/api/search", params={"q": "attention"})
        with Library(tmp_path / "library.db") as lib:
            lib.add_to_collection(["2209.15001"])
            lib.set_state("2209.15001", "processing", "Extracting")
    api2, _, _ = start(tmp_path)  # "restart the app"
    with api2:
        paper = api2.get("/api/papers/2209.15001").json()
    assert paper["status"] == "failed" and "interrupted" in paper["note"]


# ---- ask -----------------------------------------------------------------------------

QUESTION = "What PQ does DiNAT reach on COCO panoptic segmentation?"


def test_ask_before_anything_is_indexed(tmp_path):
    api, _, _ = start(tmp_path)
    with api:
        response = api.post("/api/ask", json={"question": QUESTION})
    assert response.status_code == 409 and "No indexed papers" in response.json()["detail"]


def test_ask_without_a_model_is_retrieval_only(tmp_path):
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF))
    with api:
        search_and_add(api, app)
        body = api.post("/api/ask", json={"question": QUESTION}).json()
    assert body["mode"] == "retrieval_only" and body["answer"] == ""
    assert body["notes"][0].startswith("No answer was generated: Ollama was not found")
    top = body["passages"][0]
    assert top["arxiv_id"] == "2209.15001" and top["page"] == 2
    assert top["link"] == "https://arxiv.org/pdf/2209.15001v3#page=2"
    assert "58.5 PQ" in top["text"]
    assert set(body["keywords"]) >= {"pq", "dinat", "coco", "panoptic", "segmentation"}


def test_ask_with_a_model_returns_a_checked_grounded_answer(tmp_path):
    model = FakeModel(available=True, reply="DiNAT reaches 58.5 PQ [1].")
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF), model=model)
    with api:
        search_and_add(api, app)
        body = api.post("/api/ask", json={"question": QUESTION}).json()
    assert body["mode"] == "generated"
    assert body["answer"] == "DiNAT reaches 58.5 PQ [1]."
    assert body["cited"] == [1] and body["passages"][0]["cited"] is True
    assert body["generator"] == "fake-model via Ollama"


def test_model_answer_with_fake_citation_is_discarded(tmp_path):
    model = FakeModel(available=True, reply="58.5 PQ [7].")
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF), model=model)
    with api:
        search_and_add(api, app)
        body = api.post("/api/ask", json={"question": QUESTION}).json()
    assert body["mode"] == "retrieval_only" and body["answer"] == ""
    assert "don't exist: [7]" in body["notes"][0]


def test_generation_can_be_turned_off(tmp_path):
    model = FakeModel(available=True, reply="58.5 PQ [1].")
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF), model=model)
    with api:
        search_and_add(api, app)
        body = api.post("/api/ask", json={"question": QUESTION, "generate": False}).json()
    assert body["mode"] == "retrieval_only" and body["notes"] == []


def test_unsupported_question_gets_not_enough_evidence(tmp_path):
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF))
    with api:
        search_and_add(api, app)
        body = api.post("/api/ask", json={"question": "Which GPU cluster was used to train GPT-4?"}).json()
    assert body["mode"] == "insufficient_evidence" and body["answer"] == ""
    assert body["enough_evidence"] is False
    assert "gpu" in body["missing_keywords"]


def test_ask_can_be_limited_to_one_paper_and_validates_it(tmp_path):
    api, app, _ = start(tmp_path, FakeResponse(200, SEARCH_XML), FakeResponse(200, PDF))
    with api:
        search_and_add(api, app)
        body = api.post("/api/ask", json={"question": QUESTION, "paper_id": "2209.15001"}).json()
        unknown = api.post("/api/ask", json={"question": QUESTION, "paper_id": "1111.11111"})
    assert {p["arxiv_id"] for p in body["passages"]} == {"2209.15001"}
    assert unknown.status_code == 404


def test_web_page_is_served(tmp_path):
    api, _, _ = start(tmp_path)
    with api:
        page = api.get("/")
    assert page.status_code == 200 and "Local Document Q&amp;A" in page.text
