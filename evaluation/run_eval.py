"""Measure retrieval quality and evidence detection on evaluation/questions.json.

Usage (from the project folder):
    python -m evaluation.run_eval --setup     # first time: downloads and ingests the 3 papers
    python -m evaluation.run_eval             # re-run the evaluation on the local copy

It uses its own library in data/eval/ so it never mixes with your own papers.
Metrics (all computed here, nothing is typed in by hand):
- hit@1 / hit@k: an answerable question counts as a hit if a passage within the top 1 / top k
  comes from the expected paper and page AND contains the expected evidence text.
- MRR: average of 1/rank of the first such passage (0 if none).
- evidence check: answerable questions should be judged "enough evidence";
  unanswerable ones should be refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from docqa import config
from docqa.arxiv_client import ArxivClient
from docqa.retrieval import DEFAULT_TOP_K, Passage, retrieve
from docqa.storage import FULL_TEXT, Library
from docqa.workflow import add_papers

HERE = Path(__file__).resolve().parent
QUESTIONS_FILE = HERE / "questions.json"
RESULTS_FILE = HERE / "results.md"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="download and ingest the evaluation papers first")
    parser.add_argument("--data-dir", type=Path, default=config.data_dir() / "eval")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--no-write", action="store_true", help=f"don't overwrite {RESULTS_FILE.name}")
    args = parser.parse_args(argv)

    spec = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    with Library(args.data_dir / "library.db") as library:
        if args.setup:
            for result in add_papers(ArxivClient(), library, spec["papers"], args.data_dir / "pdfs"):
                print(f"{result.arxiv_id}: {result.status} ({result.note})")
        missing = [p for p in spec["papers"] if (s := library.get_paper(p)) is None or s.ingest_status != FULL_TEXT]
        if missing:
            print(f"These papers are not ingested as full text yet: {missing}. Run with --setup.")
            return 1
        problems = check_evidence_exists(library, spec["questions"])
        if problems:
            print("The evaluation set does not match the ingested text:\n  " + "\n  ".join(problems))
            return 1
        rows = [evaluate_question(library, q, args.top) for q in spec["questions"]]
        versions = {p: library.get_paper(p).paper.version for p in spec["papers"]}

    report = format_report(rows, args.top, versions)
    print(report)
    if not args.no_write:
        RESULTS_FILE.write_text(report + "\n", encoding="utf-8")
        print(f"\nWrote {RESULTS_FILE.relative_to(HERE.parent)}")
    return 0


def check_evidence_exists(library: Library, questions: list[dict]) -> list[str]:
    """Guard against a wrong evaluation set: every evidence string must be on one of its listed pages."""
    problems = []
    for q in questions:
        for exp in q["expected"]:
            chunks = [c for c in library.get_chunks(exp["arxiv_id"]) if c.page in exp["pages"]]
            if not any(exp["evidence"] in c.text for c in chunks):
                problems.append(f"{q['id']}: '{exp['evidence']}' not found on pages {exp['pages']} of {exp['arxiv_id']}")
    return problems


def is_correct(passage: Passage, expected: list[dict]) -> bool:
    return any(
        passage.arxiv_id == exp["arxiv_id"] and passage.page in exp["pages"] and exp["evidence"] in passage.text
        for exp in expected
    )


def evaluate_question(library: Library, question: dict, top_k: int) -> dict:
    result = retrieve(library, question["question"], top_k=top_k)
    answerable = bool(question["expected"])
    first_hit = next((p.rank for p in result.passages if is_correct(p, question["expected"])), None)
    top = result.passages[0] if result.passages else None
    return {
        "id": question["id"],
        "question": question["question"],
        "answerable": answerable,
        "first_hit": first_hit,
        "enough_evidence": result.enough_evidence,
        "evidence_ok": result.enough_evidence == answerable,
        "top": f"{top.arxiv_id} p.{top.page}" if top else "-",
        "reason": result.reason,
    }


def format_report(rows: list[dict], top_k: int, versions: dict[str, str]) -> str:
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    hit1 = sum(r["first_hit"] == 1 for r in answerable)
    hitk = sum(r["first_hit"] is not None for r in answerable)
    mrr = sum(1 / r["first_hit"] for r in answerable if r["first_hit"]) / len(answerable)
    accepted = sum(r["enough_evidence"] for r in answerable)
    refused = sum(not r["enough_evidence"] for r in unanswerable)

    papers = ", ".join(f"{p}{v}" for p, v in versions.items())
    lines = [
        "# Retrieval evaluation results",
        "",
        f"Generated by `python -m evaluation.run_eval` on {date.today().isoformat()}.",
        f"Papers: {papers}. Retrieval: SQLite FTS5 BM25, top {top_k} passages.",
        "Answer generation is **not** evaluated here (it needs a local Ollama model).",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Answerable questions: correct passage ranked 1st (hit@1) | {hit1}/{len(answerable)} ({hit1 / len(answerable):.0%}) |",
        f"| Answerable questions: correct passage in top {top_k} (hit@{top_k}) | {hitk}/{len(answerable)} ({hitk / len(answerable):.0%}) |",
        f"| Mean reciprocal rank (MRR) | {mrr:.2f} |",
        f"| Answerable questions judged \"enough evidence\" | {accepted}/{len(answerable)} |",
        f"| Unanswerable questions correctly refused | {refused}/{len(unanswerable)} |",
        "",
        "## Per question",
        "",
        "| ID | Question | Answerable | Correct passage rank | Evidence judgement | Top passage |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        rank = r["first_hit"] if r["first_hit"] else ("not in top %d" % top_k if r["answerable"] else "n/a")
        judged = "enough" if r["enough_evidence"] else "not enough"
        mark = "OK" if r["evidence_ok"] else "WRONG"
        lines.append(
            f"| {r['id']} | {r['question']} | {'yes' if r['answerable'] else 'no'} | {rank} | {judged} ({mark}) | {r['top']} |"
        )
    failures = [r for r in rows if not r["evidence_ok"] or (r["answerable"] and r["first_hit"] is None)]
    if failures:
        lines += ["", "## Failures explained by the system", ""]
        for r in failures:
            lines.append(f"- **{r['id']}**: {r['reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
