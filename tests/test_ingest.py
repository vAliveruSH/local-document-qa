import dataclasses

import pytest

from conftest import FakeResponse, fixture_bytes, make_client
from docqa.arxiv_client import ArxivError, Paper
from docqa.ingest import ALREADY_INGESTED, ingest_paper
from docqa.storage import ABSTRACT_ONLY, FULL_TEXT
from docqa.workflow import FAILED, INVALID_ID, NOT_FOUND, add_papers
from pdf_maker import make_pdf

PAPER = Paper(
    arxiv_id="2401.00001", version="v1", title="A Study of Widgets", authors=("A. Author",),
    summary="We study widgets and their gears.", published="2024-01-01T00:00:00Z",
    updated="2024-01-01T00:00:00Z", abs_url="https://arxiv.org/abs/2401.00001v1",
    pdf_url="https://arxiv.org/pdf/2401.00001v1",
)
PAGE_1 = "Widgets are small mechanical parts. " * 30
PAGE_2 = "Gear ratios determine widget speed. The best ratio we found was 3 to 1. " * 20


class Downloader:
    """Fake PDF fetcher that records how often it was called."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def saved(library):
    library.save_papers([PAPER])
    return library


def test_full_text_ingest_tracks_pages(saved, tmp_path):
    result = ingest_paper(saved, PAPER, Downloader(make_pdf([PAGE_1, PAGE_2])), tmp_path)

    assert result.status == FULL_TEXT
    assert result.page_count == 2
    chunks = saved.get_chunks(PAPER.arxiv_id)
    assert result.chunk_count == len(chunks) > 0
    assert {c.page for c in chunks} == {1, 2}
    assert any("3 to 1" in c.text and c.page == 2 for c in chunks)
    stored = saved.get_paper(PAPER.arxiv_id)
    assert stored.ingest_status == FULL_TEXT and stored.page_count == 2
    assert (tmp_path / "2401.00001v1.pdf").exists()


def test_download_failure_falls_back_to_abstract_only(saved, tmp_path):
    result = ingest_paper(saved, PAPER, Downloader(ArxivError("HTTP 404")), tmp_path)

    assert result.status == ABSTRACT_ONLY
    assert "full text unavailable" in result.note and "HTTP 404" in result.note
    chunks = saved.get_chunks(PAPER.arxiv_id)
    assert len(chunks) == 1 and chunks[0].page is None
    assert "We study widgets" in chunks[0].text
    assert saved.get_paper(PAPER.arxiv_id).ingest_status == ABSTRACT_ONLY


def test_pdf_without_text_is_labelled_abstract_only(saved, tmp_path):
    result = ingest_paper(saved, PAPER, Downloader(make_pdf(["", ""])), tmp_path)
    assert result.status == ABSTRACT_ONLY
    assert "almost no extractable text" in result.note


def test_web_page_instead_of_pdf_is_abstract_only(saved, tmp_path):
    result = ingest_paper(saved, PAPER, Downloader(b"<html>Paper withdrawn</html>"), tmp_path)
    assert result.status == ABSTRACT_ONLY
    assert "not a PDF" in result.note
    assert not (tmp_path / "2401.00001v1.pdf").exists()


def test_reingesting_does_not_duplicate_chunks_or_download_again(saved, tmp_path):
    downloader = Downloader(make_pdf([PAGE_1, PAGE_2]))
    first = ingest_paper(saved, PAPER, downloader, tmp_path)
    second = ingest_paper(saved, PAPER, downloader, tmp_path)
    forced = ingest_paper(saved, PAPER, downloader, tmp_path, force=True)

    assert second.status == ALREADY_INGESTED
    assert forced.status == FULL_TEXT
    assert saved.chunk_count(PAPER.arxiv_id) == first.chunk_count
    assert downloader.calls == 2  # first ingest + forced re-ingest; the skipped one downloaded nothing
    fts_rows = saved.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    assert fts_rows == first.chunk_count


def test_abstract_only_paper_is_retried_and_upgraded(saved, tmp_path):
    ingest_paper(saved, PAPER, Downloader(ArxivError("offline")), tmp_path)
    result = ingest_paper(saved, PAPER, Downloader(make_pdf([PAGE_1])), tmp_path)
    assert result.status == FULL_TEXT
    assert all(c.page == 1 for c in saved.get_chunks(PAPER.arxiv_id))


def test_newer_version_is_ingested_again(saved, tmp_path):
    ingest_paper(saved, PAPER, Downloader(make_pdf([PAGE_1])), tmp_path)
    v2 = dataclasses.replace(PAPER, version="v2", pdf_url="https://arxiv.org/pdf/2401.00001v2")
    saved.save_papers([v2])
    result = ingest_paper(saved, v2, Downloader(make_pdf([PAGE_1, PAGE_2])), tmp_path)
    assert result.status == FULL_TEXT and result.page_count == 2


def test_add_papers_reports_every_outcome(library, tmp_path):
    # metadata lookup for the unknown IDs, then one PDF download for the paper that exists
    client, session, _ = make_client(
        FakeResponse(200, fixture_bytes("arxiv_old_style_id.xml")),
        FakeResponse(200, make_pdf(["String junctions and heterotic duals. " * 30])),
    )
    results = add_papers(client, library, ["hep-th/9901001", "2001.99999", "not an id"], tmp_path)
    by_id = {r.arxiv_id: r for r in results}

    assert by_id["not an id"].status == INVALID_ID
    assert by_id["2001.99999"].status == NOT_FOUND
    assert by_id["hep-th/9901001"].status == FULL_TEXT
    assert (tmp_path / "hep-th_9901001v3.pdf").exists()  # "/" in old IDs is made filename-safe
    assert session.calls[0]["params"]["id_list"] == "hep-th/9901001,2001.99999"


def test_add_papers_when_arxiv_is_down(library, tmp_path):
    import requests

    client, _, _ = make_client(*[requests.ConnectionError("down")] * 4)
    results = add_papers(client, library, ["1706.03762"], tmp_path)
    assert results[0].status == FAILED
    assert "could not look up metadata" in results[0].note
    assert library.list_papers() == []
