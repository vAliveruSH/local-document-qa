"""Split page text into overlapping passages ("chunks") that remember where they came from.

Chunks never cross a page boundary, so every chunk has exactly one page number to cite.
Neighbouring chunks overlap a little so a sentence cut at a chunk edge still appears whole
in one of them.
"""
from __future__ import annotations

from dataclasses import dataclass

CHUNK_WORDS = 150
OVERLAP_WORDS = 30
MIN_PAGE_WORDS = 5  # skip nearly-empty pages (e.g. a page that is only a figure)

SOURCE_PDF = "pdf"
SOURCE_ABSTRACT = "abstract"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str  # e.g. "1706.03762:p3:c7" (paper, page, chunk number)
    arxiv_id: str
    chunk_index: int  # position within the paper, starting at 0
    page: int | None  # 1-based PDF page; None for abstract text
    source: str  # SOURCE_PDF or SOURCE_ABSTRACT
    text: str


def split_words(words: list[str], size: int = CHUNK_WORDS, overlap: int = OVERLAP_WORDS) -> list[list[str]]:
    if not 0 <= overlap < size:
        raise ValueError("overlap must be at least 0 and smaller than size")
    if len(words) <= size:
        return [words] if words else []
    windows, start = [], 0
    while True:
        windows.append(words[start : start + size])
        if start + size >= len(words):
            return windows
        start += size - overlap


def chunk_pages(
    arxiv_id: str, pages: list[str], size: int = CHUNK_WORDS, overlap: int = OVERLAP_WORDS
) -> list[Chunk]:
    """pages[0] is page 1. Returns chunks in reading order."""
    chunks: list[Chunk] = []
    for page_number, text in enumerate(pages, start=1):
        words = text.split()
        if len(words) < MIN_PAGE_WORDS:
            continue
        for window in split_words(words, size, overlap):
            index = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=f"{arxiv_id}:p{page_number}:c{index}",
                    arxiv_id=arxiv_id,
                    chunk_index=index,
                    page=page_number,
                    source=SOURCE_PDF,
                    text=" ".join(window),
                )
            )
    return chunks


def abstract_chunk(arxiv_id: str, title: str, abstract: str) -> Chunk:
    """The single chunk used when only the abstract is available."""
    return Chunk(
        chunk_id=f"{arxiv_id}:abstract",
        arxiv_id=arxiv_id,
        chunk_index=0,
        page=None,
        source=SOURCE_ABSTRACT,
        text=f"{title}. {abstract}",
    )
