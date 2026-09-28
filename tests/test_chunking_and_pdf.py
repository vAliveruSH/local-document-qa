import pytest

from docqa.chunking import SOURCE_ABSTRACT, SOURCE_PDF, abstract_chunk, chunk_pages, split_words
from docqa.pdf_text import PdfProblem, check_looks_like_pdf, clean_text, extract_pages
from pdf_maker import make_pdf


def words(n, prefix="w"):
    return [f"{prefix}{i}" for i in range(n)]


def test_short_text_is_one_window():
    assert split_words(words(10), size=150, overlap=30) == [words(10)]


def test_windows_overlap_and_cover_everything():
    windows = split_words(words(300), size=150, overlap=30)
    assert [len(w) for w in windows] == [150, 150, 60]
    assert windows[0][-30:] == windows[1][:30]  # the overlap
    covered = {w for window in windows for w in window}
    assert covered == set(words(300))


def test_empty_input_gives_no_windows():
    assert split_words([]) == []


def test_bad_overlap_is_rejected():
    with pytest.raises(ValueError):
        split_words(words(10), size=10, overlap=10)


def test_chunks_keep_page_numbers_and_never_cross_pages():
    pages = [" ".join(words(200, "a")), "", "tiny", " ".join(words(40, "c"))]
    chunks = chunk_pages("1234.56789", pages, size=150, overlap=30)

    assert [c.page for c in chunks] == [1, 1, 4]  # pages 2 (empty) and 3 (under 5 words) are skipped
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert [c.chunk_id for c in chunks] == ["1234.56789:p1:c0", "1234.56789:p1:c1", "1234.56789:p4:c2"]
    assert all(c.source == SOURCE_PDF and c.arxiv_id == "1234.56789" for c in chunks)
    assert chunks[2].text.startswith("c0 ")
    assert all(not {"a0", "c0"} <= set(c.text.split()) for c in chunks)


def test_abstract_chunk_has_no_page():
    chunk = abstract_chunk("1234.56789", "A Title", "An abstract.")
    assert chunk.page is None and chunk.source == SOURCE_ABSTRACT
    assert chunk.text == "A Title. An abstract."


def test_clean_text_fixes_common_pdf_artifacts():
    raw = "The trans-\nformer uses ﬁne-tuning.\n\n  Next\tline\x00."
    assert clean_text(raw) == "The transformer uses fine-tuning. Next line."


def test_extract_pages_returns_text_per_page():
    pdf = make_pdf(["First page mentions attention.", "", "Third page mentions BLEU scores."])
    pages = extract_pages(pdf)
    assert len(pages) == 3
    assert "attention" in pages[0]
    assert pages[1].strip() == ""
    assert "BLEU" in pages[2]


def test_broken_pdf_raises_pdf_problem():
    with pytest.raises(PdfProblem):
        extract_pages(b"%PDF-1.4\nthis is not really a pdf")


def test_html_instead_of_pdf_is_detected():
    with pytest.raises(PdfProblem, match="not a PDF"):
        check_looks_like_pdf(b"<!DOCTYPE html><html>", max_bytes=1000)


def test_oversized_pdf_is_rejected():
    with pytest.raises(PdfProblem, match="larger than"):
        check_looks_like_pdf(b"%PDF-" + b"x" * 2_000_000, max_bytes=1024 * 1024)
