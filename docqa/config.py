"""Where the app keeps its local files."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Folder for the database and downloaded PDFs. Override with the DOCQA_DATA_DIR variable."""
    return Path(os.environ.get("DOCQA_DATA_DIR", PROJECT_ROOT / "data"))


def database_path() -> Path:
    return data_dir() / "library.db"


def pdf_dir() -> Path:
    return data_dir() / "pdfs"
