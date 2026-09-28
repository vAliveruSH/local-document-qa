import dataclasses

import pytest
import requests

from conftest import FakeResponse, fixture_bytes, make_client
from docqa.arxiv_client import ArxivError, parse_feed
from docqa.storage import NOT_INGESTED, Library
from docqa.workflow import LAST_SEARCH_KEY, search_and_save

PAPERS = parse_feed(fixture_bytes("arxiv_search.xml")).papers


def test_saving_twice_does_not_create_duplicates(library):
    first = library.save_papers(PAPERS)
    second = library.save_papers(PAPERS)

    assert first.new == ["2209.15001", "2605.26355"] and first.already_saved == []
    assert second.new == [] and second.already_saved == ["2209.15001", "2605.26355"]
    assert len(library.list_papers()) == 2


def test_resaving_updates_metadata_but_keeps_first_saved_time(library):
    library.save_papers(PAPERS[:1])
    before = library.get_paper("2209.15001")
    newer = dataclasses.replace(PAPERS[0], version="v4", title="Updated title")
    library.save_papers([newer])
    after = library.get_paper("2209.15001")

    assert after.paper.version == "v4"
    assert after.paper.title == "Updated title"
    assert after.first_saved_at == before.first_saved_at
    assert after.ingest_status == NOT_INGESTED


def test_round_trip_preserves_every_field(library):
    library.save_papers(PAPERS)
    assert library.get_paper("2209.15001").paper == PAPERS[0]
    assert library.get_paper("0000.00000") is None


def test_data_survives_reopening_the_database(tmp_path):
    db = tmp_path / "library.db"
    with Library(db) as lib:
        lib.save_papers(PAPERS)
        lib.set_meta("k", "v")
    with Library(db) as reopened:
        assert len(reopened.list_papers()) == 2
        assert reopened.get_meta("k") == "v"


def test_search_and_save_records_last_search_time(library):
    client, _, _ = make_client(FakeResponse(200, fixture_bytes("arxiv_search.xml")))
    outcome = search_and_save(client, library, "attention transformer", max_results=2)
    assert outcome.saved.new == ["2209.15001", "2605.26355"]
    assert library.get_meta(LAST_SEARCH_KEY)


def test_failed_refresh_does_not_remove_saved_papers(library):
    library.save_papers(PAPERS)
    library.set_meta(LAST_SEARCH_KEY, "earlier")
    client, _, _ = make_client(*[requests.ConnectionError("offline")] * 4)

    with pytest.raises(ArxivError):
        search_and_save(client, library, "attention")

    assert len(library.list_papers()) == 2
    assert library.get_meta(LAST_SEARCH_KEY) == "earlier"
