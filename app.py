"""Entry point: `python app.py --help`.

The arXiv-fetching code that used to live here now lives in docqa/arxiv_client.py,
with retries, error messages, and parsing of every metadata field (not just titles).
"""
import sys

from docqa.cli import main

if __name__ == "__main__":
    sys.exit(main())
