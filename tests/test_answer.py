import pytest
import requests

from docqa.answer import (
    GENERATED,
    INSUFFICIENT_EVIDENCE,
    RETRIEVAL_ONLY,
    answer_question,
    build_prompt,
)
from docqa.cli import format_answer
from docqa.local_llm import GeneratorError, OllamaGenerator

QUESTION = "What gear ratio is optimal for widget transmissions?"


class FakeGenerator:
    name = "fake-model"

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_default_is_clearly_labelled_retrieval_only(collection):
    answer = answer_question(collection, QUESTION)
    assert answer.mode == RETRIEVAL_ONLY
    assert answer.text == ""  # nothing generated, so no answer text exists
    output = format_answer(answer)
    assert "RETRIEVAL-ONLY RESULT - no answer was generated" in output
    assert "arXiv:2401.00001v1, page 2" in output


def test_insufficient_evidence_never_calls_the_model(collection):
    generator = FakeGenerator("Paris [1]")
    answer = answer_question(collection, "What is the capital of France?", generator)
    assert answer.mode == INSUFFICIENT_EVIDENCE
    assert generator.prompts == []
    assert "NOT ENOUGH EVIDENCE - no answer given." in format_answer(answer)


def test_generated_answer_with_valid_citations_is_accepted(collection):
    generator = FakeGenerator("The optimal ratio is three to one [1].")
    answer = answer_question(collection, QUESTION, generator)

    assert answer.mode == GENERATED
    assert answer.text == "The optimal ratio is three to one [1]."
    assert [p.chunk_id for p in answer.cited] == [answer.retrieval.passages[0].chunk_id]
    output = format_answer(answer)
    assert "GENERATED ANSWER (written by fake-model" in output
    assert "Cited: [1] arXiv:2401.00001v1, page 2" in output


def test_prompt_contains_numbered_passages_and_rules(collection):
    generator = FakeGenerator("Three to one [1].")
    answer_question(collection, QUESTION, generator)
    prompt = generator.prompts[0]
    assert "[1] (Widgets, page 2)" in prompt
    assert "three to one" in prompt
    assert "reply with exactly: INSUFFICIENT" in prompt
    assert prompt.rstrip().endswith("Answer:")


def test_answer_citing_a_nonexistent_passage_is_discarded(collection):
    answer = answer_question(collection, QUESTION, FakeGenerator("Three to one [1][9]."))
    assert answer.mode == RETRIEVAL_ONLY
    assert answer.text == ""
    assert "cited passages that don't exist: [9]" in answer.notes[0]


def test_answer_without_citations_is_discarded(collection):
    answer = answer_question(collection, QUESTION, FakeGenerator("It is three to one."))
    assert answer.mode == RETRIEVAL_ONLY
    assert "cited no passages" in answer.notes[0]


@pytest.mark.parametrize("reply", ["INSUFFICIENT", "insufficient.", "   "])
def test_model_saying_insufficient_is_respected(collection, reply):
    answer = answer_question(collection, QUESTION, FakeGenerator(reply))
    assert answer.mode == INSUFFICIENT_EVIDENCE


def test_model_failure_falls_back_to_retrieval_only(collection):
    answer = answer_question(collection, QUESTION, FakeGenerator(GeneratorError("Ollama is not running")))
    assert answer.mode == RETRIEVAL_ONLY
    assert answer.retrieval.passages
    assert "Ollama is not running" in format_answer(answer)


# ---- the Ollama client itself (HTTP is faked) -------------------------------------

class FakeOllamaSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.sent = None

    def post(self, url, json=None, timeout=None):
        self.sent = {"url": url, "json": json}
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class OllamaResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


def test_ollama_request_and_response():
    session = FakeOllamaSession(OllamaResponse(200, {"response": "Three to one [1]."}))
    generator = OllamaGenerator(model="tiny", host="http://localhost:11434", session=session)
    assert generator.generate("prompt") == "Three to one [1]."
    assert session.sent["url"] == "http://localhost:11434/api/generate"
    assert session.sent["json"]["model"] == "tiny"
    assert session.sent["json"]["options"]["temperature"] == 0


def test_ollama_not_running_gives_install_hint():
    generator = OllamaGenerator(model="tiny", session=FakeOllamaSession(requests.ConnectionError()))
    with pytest.raises(GeneratorError, match="ollama pull tiny"):
        generator.generate("prompt")


def test_ollama_missing_model_gives_pull_hint():
    generator = OllamaGenerator(model="tiny", session=FakeOllamaSession(OllamaResponse(404, {})))
    with pytest.raises(GeneratorError, match="not installed. Run: ollama pull tiny"):
        generator.generate("prompt")


def test_build_prompt_marks_abstract_only_sources(collection):
    from docqa.retrieval import retrieve

    passages = retrieve(collection, "river sediment rainfall").passages
    assert "(Rivers, abstract only)" in build_prompt("q", passages)
