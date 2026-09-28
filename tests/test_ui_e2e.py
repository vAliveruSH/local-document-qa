"""Browser end-to-end tests: a real server on a local port, driven by Playwright (Chromium).

Every screen and flow is exercised by clicking, as a user would. Only the outside world is faked:
arXiv answers come from saved fixtures, PDFs are generated, and the answer model is a stand-in.
Any JavaScript error in the page fails the test.

Needs the test browser once:  python -m playwright install chromium
"""
import re
import socket
import threading
import time

import pytest
import requests

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

import docqa.server as server_module  # noqa: E402
from conftest import FakeResponse, fixture_bytes  # noqa: E402
from docqa.arxiv_client import ArxivClient  # noqa: E402
from pdf_maker import make_pdf  # noqa: E402

SEARCH_XML = fixture_bytes("arxiv_search.xml")  # 2209.15001 (DiNAT) and 2605.26355 (Energy-gated attention)
EMPTY_XML = fixture_bytes("arxiv_id_not_found.xml")
PDF = make_pdf([
    "Dilated neighborhood attention expands receptive fields exponentially at no additional cost. " * 12,
    "DiNAT reaches 58.5 PQ on COCO panoptic segmentation with the large model variant. " * 12,
])


class FakeArxiv:
    """Answers like arXiv, chosen by URL, with switches to simulate failures."""

    def __init__(self):
        self.search_down = False
        self.missing_pdfs = {"2605.26355"}  # this PDF "404s" -> abstract only
        self.pdf_delay = 0.0

    def get(self, url, params=None, timeout=None, headers=None, stream=False):
        if "/api/query" in url:
            if self.search_down:
                raise requests.ConnectionError("offline")
            query = (params or {}).get("search_query", "")
            return FakeResponse(200, EMPTY_XML if "protein" in query else SEARCH_XML)
        if "/pdf/" in url:
            time.sleep(self.pdf_delay)
            if any(paper_id in url for paper_id in self.missing_pdfs):
                return FakeResponse(404)
            return FakeResponse(200, PDF, headers={"Content-Length": str(len(PDF))})
        raise AssertionError(f"unexpected URL {url}")


class FakeModel:
    name = "fake-model via Ollama"

    def __init__(self):
        self.available = False

    def check(self):
        return (True, self.name) if self.available else (False, "Ollama was not found at http://localhost:11434")

    def generate(self, prompt):
        return "DiNAT reaches 58.5 PQ on COCO panoptic segmentation [1]."


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app_server(tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setattr(server_module, "MODEL_CHECK_SECONDS", 0)  # see model changes immediately
    arxiv, model = FakeArxiv(), FakeModel()
    client = ArxivClient(session=arxiv, min_interval=0, sleep=lambda s: None)
    app = server_module.create_app(data_dir=tmp_path, client=client, model=model)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "arxiv": arxiv, "model": model, "app": app, "tmp": tmp_path}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"Chromium for Playwright is not installed ({exc})")
        yield b
        b.close()


@pytest.fixture
def page(browser, app_server, request):
    context = browser.new_context(viewport={"width": 1345, "height": 900})
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    # Browsers also log failed HTTP responses (e.g. the 502 a test causes on purpose); those aren't JS bugs.
    pg.on("console", lambda msg: errors.append(msg.text)
          if msg.type == "error" and not msg.text.startswith("Failed to load resource") else None)
    pg.goto(app_server["url"])
    yield pg
    context.close()
    assert errors == [], f"JavaScript errors in the page: {errors}"


def search(page, query):
    page.fill("#search-input", query)
    page.click("#search-button")


def add_both_papers(page):
    search(page, "attention transformer")
    page.click("text=Select all not in collection")
    page.click("text=Add 2 papers to collection")
    page.click("nav.nav >> text=Collection")
    expect(page.locator(".col-card")).to_have_count(2)
    expect(page.get_by_text("Indexed · abstract only")).to_be_visible(timeout=15000)
    expect(page.locator(".col-card .pill.green", has_text="Indexed")).to_be_visible(timeout=15000)


# ---- empty states ---------------------------------------------------------------------

def test_empty_states_and_services_panel(page):
    expect(page.locator("h1")).to_have_text("Discover papers")
    expect(page.locator("#service-arxiv .service-detail")).to_have_text("Ready · not contacted yet")
    expect(page.locator("#service-model .service-detail")).to_have_text("Not installed — passages only")
    page.click("nav.nav >> text=Collection")
    expect(page.get_by_text("Your collection is empty")).to_be_visible()
    page.click("nav.nav >> text=Ask")
    expect(page.get_by_text("No indexed papers yet")).to_be_visible()
    page.click("text=Find papers")
    expect(page.locator("h1")).to_have_text("Discover papers")


# ---- discover -----------------------------------------------------------------------------

def test_search_select_and_add_then_ingestion_statuses(page):
    search(page, "attention transformer")
    expect(page.locator(".paper-card")).to_have_count(2)
    expect(page.get_by_text("2 of about 18,757 results")).to_be_visible()
    first = page.locator(".paper-card").first
    expect(first.locator("h3")).to_have_text("Dilated Neighborhood Attention Transformer")
    expect(first.get_by_text("arXiv: 2209.15001")).to_be_visible()
    first.get_by_text("Show full abstract").click()
    expect(first.get_by_text("Show less")).to_be_visible()

    page.click("text=Select all not in collection")
    expect(page.get_by_text("2 papers selected")).to_be_visible()
    page.click(".selection-bar >> text=Clear")
    expect(page.locator(".selection-bar")).to_have_count(0)

    page.locator(".paper-card").nth(0).locator(".select-box").check()
    page.locator(".paper-card").nth(1).locator(".select-box").check()
    page.click("text=Add 2 papers to collection")
    expect(page.get_by_text("Added 2 papers")).to_be_visible()
    expect(page.locator(".paper-card .pill", has_text="In your collection")).to_have_count(2, timeout=15000)

    page.click("nav.nav >> text=Collection")
    expect(page.locator(".col-card")).to_have_count(2)
    expect(page.locator(".col-card .pill.green", has_text="Indexed")).to_be_visible(timeout=15000)
    expect(page.get_by_text("Indexed · abstract only")).to_be_visible(timeout=15000)
    expect(page.locator(".col-card .detail", has_text="Full text unavailable: arXiv refused the request (HTTP 404)")).to_be_visible()
    expect(page.locator(".toast", has_text="is abstract only")).to_be_visible()  # the user is told when it finishes
    expect(page.locator('[data-badge="collection"]')).to_have_text("2")
    expect(page.locator('[data-badge="ask"]')).to_have_text("2")
    expect(page.locator("#collection-stats")).to_contain_text("2indexed")


def test_search_error_then_retry(page, app_server):
    app_server["arxiv"].search_down = True
    search(page, "attention")
    expect(page.get_by_text("arXiv search didn’t complete")).to_be_visible(timeout=15000)
    expect(page.get_by_text("after 4 attempts")).to_be_visible()
    expect(page.locator("#service-arxiv .service-detail")).to_have_text("Last request failed")
    app_server["arxiv"].search_down = False
    page.get_by_role("button", name="Try again").click()
    expect(page.locator(".paper-card")).to_have_count(2)
    expect(page.locator("#service-arxiv .service-detail")).to_have_text("Connected · last search OK")


def test_search_with_no_results(page):
    search(page, "protein folding")
    expect(page.get_by_text("No papers matched “protein folding”")).to_be_visible()


# ---- collection -----------------------------------------------------------------------------

def test_collection_filters_retry_ask_shortcut_and_remove(page, app_server):
    add_both_papers(page)

    page.click('[data-chip="attention"]')
    expect(page.locator(".col-card")).to_have_count(1)
    page.click('[data-chip="all"]')
    page.fill("#collection-filter", "dilated")
    expect(page.locator(".col-card")).to_have_count(1)
    page.fill("#collection-filter", "")
    expect(page.locator(".col-card")).to_have_count(2)

    # Retry full text for the abstract-only paper once its PDF is available.
    app_server["arxiv"].missing_pdfs.clear()
    card = page.locator(".col-card", has_text="Energy-Gated Attention")
    card.get_by_text("Retry full text").click()
    expect(card.locator(".pill.green", has_text="Indexed")).to_be_visible(timeout=15000)
    expect(page.get_by_text("Indexed · abstract only")).to_have_count(0)

    # "Ask about this paper" opens Ask scoped to that paper.
    page.locator(".col-card", has_text="Dilated").get_by_text("Ask about this paper").click()
    expect(page.locator("h1")).to_have_text("Ask your collection")
    expect(page.locator("#paper-scope")).to_have_value("2209.15001")

    # Remove: cancel first, then confirm.
    page.click("nav.nav >> text=Collection")
    card = page.locator(".col-card", has_text="Dilated")
    card.get_by_text("Remove").click()
    expect(page.get_by_role("dialog")).to_contain_text("deletes its saved metadata, the downloaded PDF")
    page.click('[data-answer="no"]')
    expect(page.locator(".col-card")).to_have_count(2)
    card.get_by_text("Remove").click()
    page.click('[data-answer="yes"]')
    expect(page.locator(".col-card")).to_have_count(1)
    expect(page.get_by_text("Removed “Dilated Neighborhood Attention Transformer”")).to_be_visible()
    assert not (app_server["tmp"] / "pdfs" / "2209.15001v3.pdf").exists()


def test_download_progress_is_shown_while_ingesting(page, app_server):
    app_server["arxiv"].pdf_delay = 1.5
    search(page, "attention")
    page.locator(".paper-card").first.get_by_role("button", name="Add").click()
    page.click("nav.nav >> text=Collection")
    expect(page.locator(".banner")).to_contain_text("Ingesting 1 paper")
    expect(page.locator(".col-card .pill.blue")).to_be_visible()
    expect(page.locator(".col-card .pill.green", has_text="Indexed")).to_be_visible(timeout=15000)
    expect(page.locator(".banner")).to_have_count(0)


# ---- ask ----------------------------------------------------------------------------------------

QUESTION = "What PQ does DiNAT reach on COCO panoptic segmentation?"


def ask(page, question):
    page.fill("#question", question)
    page.keyboard.press("Control+Enter")


def test_ask_retrieval_only_and_not_enough_information(page):
    add_both_papers(page)
    page.click("nav.nav >> text=Ask")
    expect(page.locator("#paper-scope option")).to_have_count(3)  # "all" + 2 indexed papers
    expect(page.get_by_text("No answer model installed")).to_be_visible()

    ask(page, QUESTION)
    expect(page.get_by_text("Retrieved passages only")).to_be_visible()
    expect(page.get_by_text("Quoted from your papers · nothing generated")).to_be_visible()
    expect(page.get_by_text("No answer was generated: Ollama was not found")).to_be_visible()
    first = page.locator(".passage").first
    expect(first).to_contain_text("arXiv:2209.15001v3 · page 2")
    expect(first.get_by_role("link", name=re.compile("Open PDF at page 2"))).to_have_attribute(
        "href", "https://arxiv.org/pdf/2209.15001v3#page=2")

    ask(page, "Which GPU cluster was used to train GPT-4?")
    expect(page.get_by_text("Not enough information in your collection")).to_be_visible()
    expect(page.get_by_text("Rather than guess")).to_be_visible()
    expect(page.locator(".keyword-block .chip.dashed", has_text="gpu")).to_be_visible()
    if page.locator("text=Show nearest passages anyway").count():
        page.click("text=Show nearest passages anyway")
        expect(page.get_by_text("Weak matches · not evidence")).to_be_visible()

    # Recent questions are offered as one-click chips.
    expect(page.locator(".try-row [data-question]")).to_have_count(2)

    page.click("[data-action='search-arxiv']")
    expect(page.locator("h1")).to_have_text("Discover papers")
    expect(page.locator("#search-input")).to_have_value(re.compile("gpu"))
    expect(page.locator(".paper-card")).to_have_count(2)


def test_ask_with_model_shows_grounded_answer_and_citation_links(page, app_server):
    add_both_papers(page)
    app_server["model"].available = True
    page.click("nav.nav >> text=Ask")
    expect(page.locator("#service-model .service-detail")).to_have_text("fake-model via Ollama")
    expect(page.locator("#generate-toggle")).to_be_checked()

    ask(page, QUESTION)
    expect(page.get_by_text("Grounded answer")).to_be_visible()
    expect(page.locator(".answer-text")).to_contain_text("DiNAT reaches 58.5 PQ")
    expect(page.get_by_role("button", name="Cited (1)")).to_be_visible()
    expect(page.locator(".passage")).to_have_count(1)
    page.get_by_role("button", name=re.compile("All retrieved")).click()
    assert page.locator(".passage").count() >= 1
    page.click(".cite")
    expect(page.locator("#passage-1")).to_have_class(re.compile("highlight"))

    # Turning generation off gives retrieval-only results.
    page.locator("#generate-toggle").uncheck()
    ask(page, QUESTION)
    expect(page.get_by_text("Retrieved passages only")).to_be_visible()


# ---- theme and small screens -----------------------------------------------------------------------

def test_theme_choice_persists(page):
    page.click('[data-theme-choice="light"]')
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.click('[data-theme-choice="dark"]')
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_phone_layout_uses_bottom_navigation(browser, app_server):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.goto(app_server["url"])
    expect(pg.locator(".sidebar")).to_be_hidden()
    expect(pg.locator(".bottom-nav")).to_be_visible()
    pg.click(".bottom-nav >> text=Collection")
    expect(pg.locator("h1")).to_have_text("Collection")
    before = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    pg.click("#mobile-theme")
    expect(pg.locator("html")).to_have_attribute("data-theme", re.compile("light|dark"))
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") != before
    no_sideways_scroll = pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()
    assert no_sideways_scroll
    assert errors == []
