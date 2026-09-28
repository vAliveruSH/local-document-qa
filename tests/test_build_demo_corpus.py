"""The demo build script's licence check, tested offline with saved-style HTML (no arXiv calls)."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_demo_corpus.py"
spec = importlib.util.spec_from_file_location("build_demo_corpus", SCRIPT)
build_demo_corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_demo_corpus)


class FakePageClient:
    def __init__(self, html):
        self.html, self.urls = html, []

    def get(self, url, params=None, stream=False):
        self.urls.append(url)
        return type("Response", (), {"text": self.html})()


# Markup copied from arXiv's abstract page for 2310.11511v1 (fetched 2026-09-28), shortened.
def abs_page(licence_url):
    return ('<ul><li><a href="/src/2310.11511v1" class="abs-button download-eprint">TeX Source</a></li></ul>'
            f'<div class="abs-license"><a href="{licence_url}" title="Rights to this article" class="has_license">'
            '<img alt="license icon" role="presentation" src="https://arxiv.org/icons/licenses/by-4.0.png"/>'
            '<span>view license</span></a></div>'
            '<a href="https://info.arxiv.org/help/license/index.html">Copyright</a>')


def test_reads_the_licence_of_the_exact_version():
    client = FakePageClient(abs_page("http://creativecommons.org/licenses/by/4.0/"))
    assert build_demo_corpus.license_on_abs_page(client, "2310.11511", "v1") == "https://creativecommons.org/licenses/by/4.0/"
    assert client.urls == ["https://arxiv.org/abs/2310.11511v1"]


def test_reports_a_licence_that_does_not_allow_republishing():
    client = FakePageClient(abs_page("http://arxiv.org/licenses/nonexclusive-distrib/1.0/"))
    shown = build_demo_corpus.license_on_abs_page(client, "2310.11511", "v1")
    assert shown == "https://arxiv.org/licenses/nonexclusive-distrib/1.0/"
    assert shown not in build_demo_corpus.ALLOWED_LICENSES  # build() stops on this


@pytest.mark.parametrize("html", [
    "<p>no licence link here</p>",
    '<a href="https://info.arxiv.org/help/license/index.html">Copyright</a>',  # the footer link is not a licence
    abs_page("http://creativecommons.org/licenses/by/4.0/")
    + abs_page("http://arxiv.org/licenses/nonexclusive-distrib/1.0/"),
])
def test_stops_when_the_licence_is_missing_or_ambiguous(html):
    with pytest.raises(build_demo_corpus.BuildStopped):
        build_demo_corpus.license_on_abs_page(FakePageClient(html), "2310.11511", "v1")


def test_every_listed_paper_uses_a_licence_the_demo_accepts():
    from docqa.demo import ALLOWED_LICENSES

    assert build_demo_corpus.PAPERS and all(lic in ALLOWED_LICENSES for _, _, lic in build_demo_corpus.PAPERS)
