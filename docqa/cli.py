"""Command-line interface: `python app.py <command> ...`. Run `python app.py --help` for usage."""
from __future__ import annotations

import argparse
import sys
import textwrap

from . import config
from .arxiv_client import ArxivClient, ArxivError
from .ingest import ALREADY_INGESTED
from .storage import ABSTRACT_ONLY, FULL_TEXT, Library, StoredPaper
from .workflow import FAILED, INVALID_ID, LAST_SEARCH_KEY, NOT_FOUND, add_papers, search_and_save

WIDTH = 100
STATUS_LABELS = {
    FULL_TEXT: "FULL TEXT",
    ABSTRACT_ONLY: "ABSTRACT ONLY",
    ALREADY_INGESTED: "ALREADY INDEXED",
    NOT_FOUND: "NOT FOUND",
    INVALID_ID: "INVALID ID",
    FAILED: "FAILED",
}


def main(argv: list[str] | None = None) -> int:
    # Windows consoles may not support every character in paper titles; replace instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        build_parser().print_help()
        return 0
    with Library(config.database_path()) as library:
        try:
            return args.handler(args, library)
        except ArxivError as exc:
            print(f"arXiv problem: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"Input problem: {exc}", file=sys.stderr)
            return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python app.py", description="Find arXiv papers and keep them in a local library.")
    commands = parser.add_subparsers(dest="command")

    search = commands.add_parser("search", help="search arXiv and save the results' metadata")
    search.add_argument("query", help='words to search for, e.g. "retrieval augmented generation"')
    search.add_argument("--max", type=int, default=10, dest="max_results", help="number of results (1-50, default 10)")
    search.add_argument("--start", type=int, default=0, help="skip this many results (for the next page)")
    search.add_argument("--sort", choices=["relevance", "recent"], default="relevance")
    search.add_argument("--full", action="store_true", help="show whole abstracts")
    search.set_defaults(handler=cmd_search)

    add = commands.add_parser("add", help="select papers by arXiv ID and ingest their text")
    add.add_argument("ids", nargs="+", help="one or more arXiv IDs, e.g. 1706.03762")
    add.add_argument("--force", action="store_true", help="download and re-index even if already ingested")
    add.set_defaults(handler=cmd_add)

    list_cmd = commands.add_parser("list", help="show the papers saved in your library")
    list_cmd.set_defaults(handler=cmd_list)
    return parser


def cmd_search(args, library: Library) -> int:
    outcome = search_and_save(ArxivClient(), library, args.query, args.start, args.max_results, args.sort)
    page = outcome.page
    if not page.papers:
        print("arXiv returned no papers for that search. Try fewer or different words.")
        return 0
    total = f"of about {page.total_results:,} " if page.total_results is not None else ""
    print(f"Showing results {page.start + 1}-{page.start + len(page.papers)} {total}for: {args.query}\n")
    for number, paper in enumerate(page.papers, start=page.start + 1):
        is_new = paper.arxiv_id in outcome.saved.new
        print(f"[{number}] {paper.title}")
        print(f"    ID: {paper.arxiv_id}{paper.version}   {'(new)' if is_new else '(already in library)'}")
        print(f"    Authors: {format_authors(paper.authors)}")
        print(f"    Published: {paper.published[:10]}   Updated: {paper.updated[:10]}   Category: {paper.primary_category}")
        if paper.journal_ref:
            print(f"    Journal: {paper.journal_ref}")
        print(f"    Link: {paper.abs_url}")
        abstract = paper.summary if args.full else textwrap.shorten(paper.summary, 300, placeholder=" ...")
        print(textwrap.indent(textwrap.fill(abstract, WIDTH - 4), "    "))
        print()
    if page.skipped_entries:
        print(f"Note: {page.skipped_entries} result(s) were malformed and skipped.")
    print(f"Saved metadata: {len(outcome.saved.new)} new, {len(outcome.saved.already_saved)} already in your library.")
    print("Next: `python app.py add <ID>` to ingest a paper for question answering.")
    return 0


def cmd_add(args, library: Library) -> int:
    print(f"Ingesting {len(args.ids)} paper(s). PDF downloads are spaced 3 seconds apart, as arXiv requests.\n")
    results = add_papers(ArxivClient(), library, args.ids, config.pdf_dir(), force=args.force)
    for result in results:
        stored = library.get_paper(result.arxiv_id)
        title = f" {textwrap.shorten(stored.paper.title, 70, placeholder='...')}" if stored else ""
        print(f"{STATUS_LABELS.get(result.status, result.status.upper()):<16} {result.arxiv_id}{title}")
        detail = result.note
        if result.chunk_count:
            detail += f"; {result.chunk_count} searchable passage(s)"
        print(f"{'':<16} {detail}")
    counts = {status: sum(r.status == status for r in results) for status in STATUS_LABELS}
    summary = ", ".join(f"{n} {STATUS_LABELS[s].lower()}" for s, n in counts.items() if n)
    print(f"\nSummary: {summary}.")
    if counts[ABSTRACT_ONLY]:
        print("Abstract-only papers can still be searched, but answers about them rely on the abstract alone.")
    return 0 if all(r.status in (FULL_TEXT, ABSTRACT_ONLY, ALREADY_INGESTED) for r in results) else 1


def cmd_list(args, library: Library) -> int:
    papers = library.list_papers()
    if not papers:
        print("Your library is empty. Start with: python app.py search \"your topic\"")
        return 0
    print(f"{len(papers)} paper(s) in {config.database_path()}")
    print(f"{library.chunk_count()} searchable passage(s) in the index\n")
    print(f"{'ID':<14} {'STATUS':<13} {'PAGES':>5} {'CHUNKS':>6}  TITLE")
    for stored in papers:
        print(format_library_line(stored, library.chunk_count(stored.paper.arxiv_id)))
        if stored.ingest_status == ABSTRACT_ONLY:
            print(f"{'':<14} reason: {stored.ingest_note}")
        elif stored.ingested_version and stored.ingested_version != stored.paper.version:
            print(f"{'':<14} note: {stored.ingested_version} is indexed; {stored.paper.version} is newer "
                  f"(run `python app.py add {stored.paper.arxiv_id} --force` to update)")
    print(f"\nLast arXiv search: {library.get_meta(LAST_SEARCH_KEY) or 'never'}")
    return 0


def format_authors(authors: tuple[str, ...], limit: int = 3) -> str:
    if not authors:
        return "(no authors listed)"
    shown = ", ".join(authors[:limit])
    return f"{shown} et al. ({len(authors)} authors)" if len(authors) > limit else shown


def format_library_line(stored: StoredPaper, chunks: int) -> str:
    paper = stored.paper
    pages = stored.page_count if stored.page_count is not None else "-"
    title = textwrap.shorten(paper.title, 60, placeholder="...")
    return f"{paper.arxiv_id:<14} {stored.ingest_status:<13} {pages:>5} {chunks:>6}  {title}"
