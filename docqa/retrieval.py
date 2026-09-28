"""Find the passages in the local library that best match a question.

Approach: keyword search with BM25 ranking, using SQLite's built-in FTS5 index.
- No extra downloads or models; the index is stored in the same SQLite file and survives restarts.
- BM25 rewards passages that contain the question's rarer words, several times.
- It is easy to inspect: we can show exactly which question words each passage matched.
Weakness: it can't match synonyms ("car" vs "automobile"); see the README's limitations.

The "enough evidence" check is deliberately simple: the best passage must contain enough of
the question's keywords. If it doesn't, the app says so instead of answering.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .storage import Library

DEFAULT_TOP_K = 5
# The best passage must contain at least this share of the question's keywords.
MIN_KEYWORD_COVERAGE = 0.6

STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because been before being
    below between both but by can could did do does doing down during each few for from further had has
    have having he her here hers him his how i if in into is it its itself just me more most my no nor
    not now of off on once only or other our ours out over own same she should so some such than that
    the their theirs them then there these they this those through to too under until up very was we
    were what when where which while who whom why will with would you your yours
    describe explain paper papers tell show give according mentioned say says said
    many much use uses used using""".split()
)


@dataclass(frozen=True)
class Passage:
    rank: int
    chunk_id: str
    arxiv_id: str
    version: str
    title: str
    page: int | None
    source: str
    text: str
    score: float  # BM25 relevance; higher is better
    matched_keywords: tuple[str, ...]
    abs_url: str
    pdf_url: str

    @property
    def location(self) -> str:
        return f"page {self.page}" if self.page is not None else "abstract only"

    @property
    def citation(self) -> str:
        return f"arXiv:{self.arxiv_id}{self.version}, {self.location}"

    @property
    def link(self) -> str:
        return f"{self.pdf_url}#page={self.page}" if self.page is not None else self.abs_url


@dataclass(frozen=True)
class RetrievalResult:
    question: str
    keywords: tuple[str, ...]
    passages: list[Passage]
    enough_evidence: bool
    reason: str


def extract_keywords(question: str) -> list[str]:
    """Lower-case content words from the question, in order, without stopwords or repeats."""
    keywords = []
    for word in re.findall(r"[a-z0-9]+", question.lower()):
        if word in STOPWORDS or (len(word) < 2 and not word.isdigit()):
            continue
        if word not in keywords:
            keywords.append(word)
    return keywords


def keywords_needed(keyword_count: int) -> int:
    return max(1, math.ceil(keyword_count * MIN_KEYWORD_COVERAGE))


def retrieve(library: Library, question: str, top_k: int = DEFAULT_TOP_K, arxiv_id: str | None = None) -> RetrievalResult:
    keywords = extract_keywords(question)
    if not keywords:
        return RetrievalResult(question, (), [], False, "The question has no searchable words.")

    # Each keyword is quoted, so characters like ' or ( in the question can't break the FTS5 syntax.
    rows = library.search_chunks(" OR ".join(f'"{k}"' for k in keywords), top_k, arxiv_id)
    if not rows:
        return RetrievalResult(
            question, tuple(keywords), [], False,
            "No passage in your library contains any of the words: " + ", ".join(keywords) + ".",
        )

    chunk_ids = [row["chunk_id"] for row in rows]
    matches = {k: library.chunks_containing(f'"{k}"', chunk_ids) for k in keywords}
    passages = [
        Passage(
            rank=rank,
            chunk_id=row["chunk_id"],
            arxiv_id=row["arxiv_id"],
            version=row["version"],
            title=row["title"],
            page=row["page"],
            source=row["source"],
            text=row["text"],
            score=round(-row["bm25"], 3),
            matched_keywords=tuple(k for k in keywords if row["chunk_id"] in matches[k]),
            abs_url=row["abs_url"],
            pdf_url=row["pdf_url"],
        )
        for rank, row in enumerate(rows, start=1)
    ]

    best = max(len(p.matched_keywords) for p in passages)
    needed = keywords_needed(len(keywords))
    if best < needed:
        missing = [k for k in keywords if not any(k in p.matched_keywords for p in passages)]
        reason = (
            f"The best passage contains only {best} of the {len(keywords)} key words in your question "
            f"(at least {needed} needed)."
        )
        if missing:
            reason += " Not found in any top passage: " + ", ".join(missing) + "."
        return RetrievalResult(question, tuple(keywords), passages, False, reason)

    return RetrievalResult(
        question, tuple(keywords), passages, True,
        f"The best passage contains {best} of the {len(keywords)} key words in your question.",
    )
