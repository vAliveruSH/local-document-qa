"""Optional answer generation with a free local model served by Ollama (https://ollama.com).

Nothing here is required: without Ollama the app runs in retrieval-only mode.
"""
from __future__ import annotations

import os

import requests

DEFAULT_MODEL = os.environ.get("DOCQA_OLLAMA_MODEL", "llama3.2:3b")
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class GeneratorError(Exception):
    """The local model could not produce an answer."""


class OllamaGenerator:
    def __init__(self, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST, timeout: float = 300.0, session=None):
        self.model = model
        self.host = host if host.startswith("http") else f"http://{host}"
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def name(self) -> str:
        return f"{self.model} via Ollama"

    def check(self) -> tuple[bool, str]:
        """Is Ollama running with this model installed? Returns (available, explanation). Never raises."""
        try:
            response = self.session.get(f"{self.host}/api/tags", timeout=1.5)
            names = {m.get("name", "") for m in response.json().get("models", [])}
        except (requests.RequestException, ValueError, AttributeError):
            return False, f"Ollama was not found at {self.host}"
        if self.model in names or f"{self.model}:latest" in names:
            return True, f"{self.model} via Ollama"
        return False, f"Ollama is running, but the model '{self.model}' is not installed (ollama pull {self.model})"

    def generate(self, prompt: str) -> str:
        try:
            response = self.session.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": 0}},
                timeout=self.timeout,
            )
        except requests.ConnectionError as exc:
            raise GeneratorError(
                f"Ollama is not running at {self.host}. Install it from https://ollama.com, "
                f"then run: ollama pull {self.model}"
            ) from exc
        except requests.Timeout as exc:
            raise GeneratorError(f"the model took longer than {self.timeout:.0f} seconds") from exc
        if response.status_code == 404:
            raise GeneratorError(f"the model '{self.model}' is not installed. Run: ollama pull {self.model}")
        if response.status_code != 200:
            raise GeneratorError(f"Ollama returned HTTP {response.status_code}")
        try:
            return str(response.json().get("response", ""))
        except ValueError as exc:
            raise GeneratorError("Ollama sent a response that is not JSON") from exc
