"""Get clean text out of PDF files, one string per page (so we can cite page numbers)."""
from __future__ import annotations

import io
import logging
import re
import unicodedata

from pypdf import PdfReader

# pypdf prints many harmless warnings about imperfect PDFs; only show real errors.
logging.getLogger("pypdf").setLevel(logging.ERROR)


class PdfProblem(Exception):
    """The file is not a usable PDF."""


def check_looks_like_pdf(data: bytes, max_bytes: int) -> None:
    if not data.startswith(b"%PDF-"):
        raise PdfProblem("the downloaded file is not a PDF (arXiv may have sent a web page instead)")
    if len(data) > max_bytes:
        raise PdfProblem(f"the PDF is larger than {max_bytes // (1024 * 1024)} MB")


def extract_pages(data: bytes) -> list[str]:
    """Return raw text for each page. A page whose text can't be read becomes ''."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")  # many "encrypted" PDFs open with an empty password
        pages = list(reader.pages)
    except Exception as exc:  # pypdf raises many different error types for broken files
        raise PdfProblem(f"the PDF could not be opened ({exc.__class__.__name__}: {exc})") from exc

    texts = []
    for page in pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    return texts


def clean_text(text: str) -> str:
    """Normalise extracted text so it searches and reads well."""
    text = unicodedata.normalize("NFKC", text)  # e.g. the ligature "ﬁ" becomes "fi"
    text = text.replace("\x00", "")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # re-join words hyphenated across lines
    text = re.sub(r"\s+", " ", text)
    return text.strip()
