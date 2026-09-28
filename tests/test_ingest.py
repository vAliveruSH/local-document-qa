import dataclasses

import pytest

from conftest import FakeResponse, fixture_bytes, make_client
from docqa.arxiv_client import ArxivError, Paper
from docqa.ingest import ALREADY_INGESTED, ingest_paper
from docqa.storage import ABSTRACT_ONLY, FAILED, FULL_TEXT, QUEUED
from docqa.workflow import INVALID_ID, NOT_FOUND, add_papers, queue_for_ingest, remove_paper, select_papers
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

    def __call__(self, url, on_progress=None):
        self.calls += 1
        if on_progress:
            on_progress(50)
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


def test_unexpected_error_is_recorded_as_failed(saved, tmp_path):
    def broken(url, on_progress=None):
        raise OSError("disk full")

    result = ingest_paper(saved, PAPER, broken, tmp_path)
    assert result.status == FAILED
    stored = saved.get_paper(PAPER.arxiv_id)
    assert stored.ingest_status == FAILED and "disk full" in stored.ingest_note


def test_download_progress_is_stored_while_downloading(saved, tmp_path):
    seen = []

    def fetch(url, on_progress):
        on_progress(40)
        seen.append(saved.get_paper(PAPER.arxiv_id))
        return make_pdf([PAGE_1])

    ingest_paper(saved, PAPER, fetch, tmp_path)
    assert seen[0].ingest_status == "downloading" and seen[0].progress == 40
    assert saved.get_paper(PAPER.arxiv_id).progress is None  # cleared when finished


def test_select_adds_to_collection_and_queue_marks_status(library, tmp_path):
    library.save_papers([PAPER])
    assert library.stats()["papers"] == 0  # a search result is not in the collection yet
    client, _, _ = make_client()
    selection = select_papers(client, library, [PAPER.arxiv_id, "bad id"])
    queue_for_ingest(library, selection.ready)

    assert selection.ready == [PAPER.arxiv_id]
    assert selection.problems[0].status == INVALID_ID
    stored = library.get_paper(PAPER.arxiv_id)
    assert stored.in_collection and stored.ingest_status == QUEUED
    assert library.stats()["in_progress"] == 1


def test_remove_paper_deletes_metadata_chunks_and_pdfs(saved, tmp_path):
    ingest_paper(saved, PAPER, Downloader(make_pdf([PAGE_1, PAGE_2])), tmp_path)
    (tmp_path / "2401.00001v0.pdf").write_bytes(b"%PDF-old version")
    (tmp_path / "2401.000011v1.pdf").write_bytes(b"%PDF-a different paper")
    chunks = saved.chunk_count(PAPER.arxiv_id)

    removal = remove_paper(saved, PAPER.arxiv_id, tmp_path)

    assert removal.chunks_deleted == chunks and removal.pdfs_deleted == 2
    assert saved.get_paper(PAPER.arxiv_id) is None
    assert saved.chunk_count() == 0
    assert saved.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert (tmp_path / "2401.000011v1.pdf").exists()  # other papers' files are untouched


def test_remove_refuses_while_ingesting(saved, tmp_path):
    queue_for_ingest(saved, [PAPER.arxiv_id])
    with pytest.raises(ValueError, match="still being ingested"):
        remove_paper(saved, PAPER.arxiv_id, tmp_path)


def test_interrupted_ingestion_becomes_failed_on_restart(saved):
    queue_for_ingest(saved, [PAPER.arxiv_id])
    assert saved.mark_interrupted() == 1
    stored = saved.get_paper(PAPER.arxiv_id)
    assert stored.ingest_status == FAILED and "interrupted" in stored.ingest_note
