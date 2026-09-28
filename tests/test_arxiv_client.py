import pytest
import requests

from conftest import FakeResponse, fixture_bytes, make_client
from docqa.arxiv_client import (
    ArxivError,
    build_search_query,
    normalize_arxiv_id,
    parse_feed,
)


# ---- parsing real arXiv responses (saved in tests/fixtures) ------------------

def test_parse_search_results_reads_all_metadata():
    page = parse_feed(fixture_bytes("arxiv_search.xml"))

    assert page.total_results == 18757
    assert page.start == 0
    assert [p.arxiv_id for p in page.papers] == ["2209.15001", "2605.26355"]
    first = page.papers[0]
    assert first.version == "v3"
    assert first.title == "Dilated Neighborhood Attention Transformer"
    assert first.authors == ("Ali Hassani", "Humphrey Shi")
    assert first.summary.startswith("Transformers are quickly becoming")
    assert first.published == "2022-09-29T17:57:08Z"
    assert first.abs_url == "https://arxiv.org/abs/2209.15001v3"
    assert first.pdf_url == "https://arxiv.org/pdf/2209.15001v3"
    assert first.primary_category == "cs.CV"
    assert first.categories == ("cs.CV", "cs.AI", "cs.LG")


def test_parse_old_style_id_with_journal_and_doi():
    paper = parse_feed(fixture_bytes("arxiv_old_style_id.xml")).papers[0]
    assert paper.arxiv_id == "hep-th/9901001"
    assert paper.version == "v3"
    assert paper.journal_ref == "Prog.Theor.Phys.101:1155-1164,1999"
    assert paper.doi == "10.1143/PTP.101.1155"
    # Whitespace inside the abstract is normalised.
    assert paper.summary.startswith("We explicitly give")


def test_arxiv_error_entry_becomes_arxiv_error():
    with pytest.raises(ArxivError, match="incorrect id format"):
        parse_feed(fixture_bytes("arxiv_error_bad_id.xml"))


def test_unknown_id_gives_empty_result_not_error():
    page = parse_feed(fixture_bytes("arxiv_id_not_found.xml"))
    assert page.papers == []
    assert page.total_results == 0


@pytest.mark.parametrize("bad", [b"", b"<html>Service unavailable</html", b"not xml at all"])
def test_malformed_xml_raises_arxiv_error(bad):
    with pytest.raises(ArxivError):
        parse_feed(bad)


def test_non_atom_xml_raises_arxiv_error():
    with pytest.raises(ArxivError, match="not an Atom feed"):
        parse_feed(b"<html><body>maintenance</body></html>")


def test_entries_missing_id_or_title_are_skipped_and_counted():
    xml = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>http://arxiv.org/abs/1706.03762v7</id><title></title></entry>
      <entry><title>No id here</title></entry>
      <entry><id>http://arxiv.org/abs/1810.04805v2</id><title>BERT</title></entry>
    </feed>"""
    page = parse_feed(xml)
    assert [p.arxiv_id for p in page.papers] == ["1810.04805"]
    assert page.skipped_entries == 2
    # Missing optional fields fall back to safe defaults.
    assert page.papers[0].authors == ()
    assert page.papers[0].pdf_url == "https://arxiv.org/pdf/1810.04805v2"


# ---- turning user input into arXiv syntax -------------------------------------

def test_plain_words_must_all_match():
    assert build_search_query("  retrieval   augmented generation ") == (
        "all:retrieval AND all:augmented AND all:generation"
    )


def test_quoted_phrase_is_kept_together():
    assert build_search_query('"large language model" hallucination?') == (
        'all:"large language model" AND all:hallucination'
    )


def test_arxiv_field_syntax_passes_through():
    assert build_search_query("ti:transformer AND cat:cs.CL") == "ti:transformer AND cat:cs.CL"


@pytest.mark.parametrize("empty", ["", "   ", "?!"])
def test_empty_query_is_rejected(empty):
    with pytest.raises(ValueError):
        build_search_query(empty)


@pytest.mark.parametrize(
    "raw",
    ["1706.03762", "1706.03762v7", "arXiv:1706.03762", "https://arxiv.org/abs/1706.03762v2",
     "https://arxiv.org/pdf/1706.03762v7.pdf"],
)
def test_id_normalisation(raw):
    assert normalize_arxiv_id(raw) == "1706.03762"


def test_old_style_id_normalisation():
    assert normalize_arxiv_id("hep-th/9901001v3") == "hep-th/9901001"


@pytest.mark.parametrize("bad", ["", "attention", "1706.037", "1706-03762"])
def test_invalid_ids_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_arxiv_id(bad)


# ---- HTTP behaviour: waits, retries, error messages ----------------------------

def test_search_sends_expected_parameters_and_user_agent():
    client, session, _ = make_client(FakeResponse(200, fixture_bytes("arxiv_search.xml")))
    page = client.search("attention transformer", max_results=2, sort="recent")

    assert len(page.papers) == 2
    call = session.calls[0]
    assert call["params"]["search_query"] == "all:attention AND all:transformer"
    assert call["params"]["max_results"] == 2
    assert call["params"]["sortBy"] == "submittedDate"
    assert "local-document-qa" in call["headers"]["User-Agent"]
    assert call["timeout"] == 30.0


def test_retries_after_intermittent_406_then_succeeds():
    client, session, sleeps = make_client(
        FakeResponse(406), FakeResponse(503), FakeResponse(200, fixture_bytes("arxiv_search.xml"))
    )
    page = client.search("attention")
    assert len(page.papers) == 2
    assert len(session.calls) == 3
    # Backoff waits grow: 3 s then 6 s (plus the polite 3 s gap before each request).
    assert 3.0 in sleeps and 6.0 in sleeps


def test_gives_up_with_clear_message_after_retries():
    client, session, _ = make_client(*[requests.ConnectionError("down")] * 4)
    with pytest.raises(ArxivError, match="after 4 attempts.*network error"):
        client.search("attention")
    assert len(session.calls) == 4


def test_timeout_is_reported_in_plain_words():
    client, _, _ = make_client(*[requests.Timeout()] * 4)
    with pytest.raises(ArxivError, match="no response within 30 seconds"):
        client.search("attention")


def test_non_retryable_status_fails_immediately():
    client, session, _ = make_client(FakeResponse(400))
    with pytest.raises(ArxivError, match="HTTP 400"):
        client.search("attention")
    assert len(session.calls) == 1


def test_waits_at_least_three_seconds_between_requests():
    ok = FakeResponse(200, fixture_bytes("arxiv_search.xml"))
    client, _, sleeps = make_client(ok, ok)
    client.search("a")
    client.search("b")
    assert sleeps == [3.0]  # the fake clock never advances, so the second call waits the full gap


def test_retry_after_header_is_respected():
    client, _, sleeps = make_client(
        FakeResponse(429, headers={"Retry-After": "20"}), FakeResponse(200, fixture_bytes("arxiv_search.xml"))
    )
    client.search("attention")
    assert 20.0 in sleeps


def test_fetch_by_ids_uses_id_list():
    client, session, _ = make_client(FakeResponse(200, fixture_bytes("arxiv_old_style_id.xml")))
    papers = client.fetch_by_ids(["hep-th/9901001"])
    assert papers[0].arxiv_id == "hep-th/9901001"
    assert session.calls[0]["params"]["id_list"] == "hep-th/9901001"


@pytest.mark.parametrize("kwargs", [{"max_results": 0}, {"max_results": 51}, {"start": -1}, {"sort": "random"}])
def test_invalid_search_options_are_rejected(kwargs):
    client, session, _ = make_client()
    with pytest.raises(ValueError):
        client.search("attention", **kwargs)
    assert session.calls == []
