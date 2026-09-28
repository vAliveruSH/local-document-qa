import pytest

from docqa.arxiv_client import Paper
from docqa.chunking import abstract_chunk, chunk_pages
from docqa.retrieval import extract_keywords, keywords_needed, retrieve
from docqa.storage import ABSTRACT_ONLY, FULL_TEXT, Library


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


def test_keywords_drop_stopwords_punctuation_and_repeats():
    assert extract_keywords("What is the gear ratio? What's THE ratio used for widgets?") == [
        "gear", "ratio", "widgets"
    ]


@pytest.mark.parametrize("count, needed", [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (10, 6)])
def test_keywords_needed(count, needed):
    assert keywords_needed(count) == needed


def test_relevant_passage_ranks_first_with_page_number(collection):
    result = retrieve(collection, "What gear ratio is optimal for widget transmissions?")

    assert result.enough_evidence
    top = result.passages[0]
    assert top.arxiv_id == "2401.00001" and top.page == 2
    assert "three to one" in top.text
    assert top.citation == "arXiv:2401.00001v1, page 2"
    assert top.link == "https://arxiv.org/pdf/2401.00001v1#page=2"
    assert set(top.matched_keywords) >= {"gear", "ratio", "transmissions"}


def test_stemming_matches_word_variants(collection):
    result = retrieve(collection, "How much sunlight do tomatoes need?")
    assert result.passages[0].arxiv_id == "2401.00002"
    assert "tomatoes" in result.passages[0].matched_keywords  # "tomatoes" matched "Tomato"


def test_abstract_only_passage_is_labelled(collection):
    result = retrieve(collection, "river sediment transport after rainfall")
    top = result.passages[0]
    assert top.page is None and top.source == "abstract"
    assert top.citation == "arXiv:2401.00003v1, abstract only"
    assert top.link == "https://arxiv.org/abs/2401.00003v1"


def test_unrelated_question_has_insufficient_evidence(collection):
    result = retrieve(collection, "What is the capital of France?")
    assert not result.enough_evidence
    assert result.passages == []
    assert "capital, france" in result.reason


def test_partial_match_is_not_enough_evidence(collection):
    # Shares one word ("widgets") with the library but asks about something it doesn't contain.
    result = retrieve(collection, "What colour paint do widgets need for underwater corrosion?")
    assert not result.enough_evidence
    assert result.passages  # the weak matches are still returned so the user can see why
    assert "only 1 of" in result.reason


def test_question_without_content_words(collection):
    result = retrieve(collection, "What is it?")
    assert not result.enough_evidence and result.reason == "The question has no searchable words."


@pytest.mark.parametrize("tricky", ['gear "ratio', "gear AND OR NOT (ratio*", "gear's ratio -- NEAR/3 {x}"])
def test_special_characters_do_not_break_search(collection, tricky):
    result = retrieve(collection, tricky)
    assert result.passages[0].arxiv_id == "2401.00001"


def test_search_can_be_limited_to_one_paper(collection):
    result = retrieve(collection, "widgets tomato sunlight", arxiv_id="2401.00001")
    assert {p.arxiv_id for p in result.passages} == {"2401.00001"}


def test_top_k_limits_results(collection):
    assert len(retrieve(collection, "widgets gardens rivers", top_k=2).passages) == 2


def test_empty_library(tmp_path):
    with Library(tmp_path / "empty.db") as lib:
        result = retrieve(lib, "gear ratio")
        assert not result.enough_evidence and result.passages == []


def test_index_survives_reopening(tmp_path, collection):
    collection.close()
    with Library(tmp_path / "library.db") as reopened:
        assert retrieve(reopened, "optimal gear ratio").passages[0].page == 2
