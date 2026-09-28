"""Build the public demo's bundled corpus: demo/library.db and demo/corpus.json.

Run once, on your own computer, whenever the demo papers change:

    python scripts/build_demo_corpus.py

For each paper below it:
  1. reads the arXiv page of that exact version and stops unless it shows the expected
     licence (only licences that allow republishing the text are accepted),
  2. fetches the metadata of that exact version from the arXiv API,
  3. downloads the PDF into a temporary folder outside the project, extracts and indexes the
     text with the same code as the local app, and stops unless the full text was extracted,
  4. bundles a read-only database with no PDFs, file paths or search history, and writes
     corpus.json with the licence and attribution of every paper.

The temporary folder, including the PDFs, is deleted at the end. The deployed demo never
runs this script and never contacts arXiv.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docqa.arxiv_client import ArxivClient  # noqa: E402
from docqa.demo import ALLOWED_LICENSES, CHANGES_NOTE, bundle_database, check_corpus, demo_dir, load_manifest  # noqa: E402
from docqa.ingest import ingest_paper  # noqa: E402
from docqa.storage import FULL_TEXT, Library  # noqa: E402

CC_BY_4 = "https://creativecommons.org/licenses/by/4.0/"

# (arXiv ID, exact version, licence that version's arXiv page must show)
PAPERS = [
    ("2310.11511", "v1", CC_BY_4),  # Self-RAG
    ("2109.11085", "v1", CC_BY_4),  # Towards Universal Dense Retrieval for Open-domain QA
    ("2408.08444", "v2", CC_BY_4),  # W-RAG
    ("2501.15915", "v1", CC_BY_4),  # Parametric Retrieval Augmented Generation
]

# arXiv's abstract page shows the licence as <div class="abs-license"><a href="LICENCE URL" ...>.
LICENSE_LINK = re.compile(r'<div[^>]*class="abs-license"[^>]*>\s*<a[^>]*href="([^"]+)"')


class BuildStopped(Exception):
    pass


def license_on_abs_page(client: ArxivClient, arxiv_id: str, version: str) -> str:
    html = client.get(f"https://arxiv.org/abs/{arxiv_id}{version}").text
    found = {url.replace("http://", "https://", 1) for url in LICENSE_LINK.findall(html)}
    if len(found) != 1:
        raise BuildStopped(f"{arxiv_id}{version}: could not find exactly one licence link on its arXiv page")
    url = found.pop()
    return url if url.endswith("/") else url + "/"


def build(output: Path) -> None:
    client = ArxivClient()

    for arxiv_id, version, expected in PAPERS:
        if expected not in ALLOWED_LICENSES:
            raise BuildStopped(f"{arxiv_id}{version}: {expected} is not a licence the demo accepts")
        shown = license_on_abs_page(client, arxiv_id, version)
        if shown != expected:
            raise BuildStopped(f"{arxiv_id}{version}: its arXiv page shows {shown}, expected {expected}")
        print(f"licence ok   {arxiv_id}{version}  {ALLOWED_LICENSES[shown]}")

    wanted = {arxiv_id: version for arxiv_id, version, _ in PAPERS}
    papers = client.fetch_by_ids([f"{i}{v}" for i, v in wanted.items()])
    by_id = {p.arxiv_id: p for p in papers}
    for arxiv_id, version in wanted.items():
        if arxiv_id not in by_id or by_id[arxiv_id].version != version:
            raise BuildStopped(f"{arxiv_id}{version}: arXiv did not return metadata for this exact version")

    with tempfile.TemporaryDirectory(prefix="docqa-demo-build-") as tmp:
        work = Path(tmp)
        source = work / "source.db"
        with Library(source) as library:
            library.save_papers(papers)
            library.add_to_collection(list(wanted))
            for arxiv_id in wanted:
                result = ingest_paper(library, by_id[arxiv_id], client.download, work / "pdfs")
                if result.status != FULL_TEXT:
                    raise BuildStopped(f"{arxiv_id}: full text was not extracted ({result.note})")
                print(f"indexed      {arxiv_id}{wanted[arxiv_id]}  {result.page_count} pages, {result.chunk_count} passages")

        output.mkdir(parents=True, exist_ok=True)
        bundle_database(source, output / "library.db")

    manifest = {
        "about": "Papers bundled in the public read-only demo. Each exact version is licensed as listed; "
                 "the licence is shown on that version's arXiv page (source).",
        "changes": CHANGES_NOTE,
        "papers": [
            {
                "arxiv_id": arxiv_id,
                "version": version,
                "title": by_id[arxiv_id].title,
                "authors": list(by_id[arxiv_id].authors),
                "source": f"https://arxiv.org/abs/{arxiv_id}{version}",
                "license": ALLOWED_LICENSES[expected],
                "license_url": expected,
            }
            for arxiv_id, version, expected in PAPERS
        ],
    }
    (output / "corpus.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with Library.open_read_only(output / "library.db") as library:  # the same checks the demo runs
        check_corpus(library, load_manifest(output / "corpus.json"))
        stats = library.stats()
    size_kb = (output / "library.db").stat().st_size // 1024
    print(f"built        {output / 'library.db'} ({size_kb} KB) and corpus.json; {stats}")


if __name__ == "__main__":
    try:
        build(demo_dir())
    except BuildStopped as exc:
        sys.exit(f"Stopped, nothing was published: {exc}")
