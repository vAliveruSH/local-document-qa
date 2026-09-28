"""Turn retrieved passages into a result the user can trust.

Three possible modes:
- insufficient_evidence: the library doesn't support an answer; say so.
- retrieval_only (default): show the best passages. No text is generated, and the output
  says so.
- generated: a local model wrote an answer from the passages. It is only accepted if it
  cites passages by number and every cited number is a real retrieved passage.

Retrieval never depends on generation, so the app works without any model installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from .local_llm import GeneratorError
from .retrieval import DEFAULT_TOP_K, Passage, RetrievalResult, retrieve
from .storage import Library

INSUFFICIENT_EVIDENCE = "insufficient_evidence"
RETRIEVAL_ONLY = "retrieval_only"
GENERATED = "generated"

INSUFFICIENT_MARKER = "INSUFFICIENT"
CITATION = re.compile(r"\[(\d+)\]")


class Generator(Protocol):
    name: str

    def generate(self, prompt: str) -> str: ...


@dataclass
class Answer:
    mode: str
    retrieval: RetrievalResult
    text: str = ""  # only set in GENERATED mode
    cited: list[Passage] = field(default_factory=list)  # passages the generated text cites
    notes: list[str] = field(default_factory=list)
    generator_name: str = ""


def answer_question(
    library: Library,
    question: str,
    generator: Generator | None = None,
    top_k: int = DEFAULT_TOP_K,
    arxiv_id: str | None = None,
) -> Answer:
    retrieval = retrieve(library, question, top_k=top_k, arxiv_id=arxiv_id)
    if not retrieval.enough_evidence:
        return Answer(INSUFFICIENT_EVIDENCE, retrieval, notes=[retrieval.reason])
    if generator is None:
        return Answer(RETRIEVAL_ONLY, retrieval)
    try:
        raw = generator.generate(build_prompt(question, retrieval.passages))
    except GeneratorError as exc:
        return Answer(RETRIEVAL_ONLY, retrieval, notes=[f"No answer was generated: {exc}."])
    return check_generated_answer(raw, retrieval, generator.name)


def build_prompt(question: str, passages: list[Passage]) -> str:
    sources = "\n\n".join(f"[{p.rank}] ({p.title}, {p.location})\n{p.text}" for p in passages)
    return f"""You answer questions about research papers using ONLY the numbered passages below.

Rules:
- Use only facts stated in the passages. Do not use outside knowledge.
- After every sentence, cite the passage(s) it relies on, like [1] or [2][3].
- If the passages do not contain the answer, reply with exactly: {INSUFFICIENT_MARKER}
- Keep the answer under 120 words.

Passages:

{sources}

Question: {question}
Answer:"""


def check_generated_answer(raw: str, retrieval: RetrievalResult, generator_name: str) -> Answer:
    """Accept the model's text only if its citations all point to retrieved passages."""
    text = raw.strip()
    if not text or text.upper().startswith(INSUFFICIENT_MARKER):
        return Answer(
            INSUFFICIENT_EVIDENCE, retrieval, generator_name=generator_name,
            notes=[f"The local model ({generator_name}) judged that the passages do not answer the question."],
        )
    cited_numbers = {int(n) for n in CITATION.findall(text)}
    valid = {p.rank: p for p in retrieval.passages}
    if not cited_numbers:
        return Answer(
            RETRIEVAL_ONLY, retrieval, generator_name=generator_name,
            notes=["The local model's answer was discarded because it cited no passages."],
        )
    unknown = sorted(cited_numbers - valid.keys())
    if unknown:
        return Answer(
            RETRIEVAL_ONLY, retrieval, generator_name=generator_name,
            notes=[f"The local model's answer was discarded because it cited passages that don't exist: {unknown}."],
        )
    return Answer(
        GENERATED, retrieval, text=text, generator_name=generator_name,
        cited=[valid[n] for n in sorted(cited_numbers)],
    )
