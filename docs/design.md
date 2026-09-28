# AI Research Radar

## Goal
Discover and search recent AI research papers without downloading a large PDF collection.

## First version
Fetch paper metadata from arXiv, save it locally, and show searchable results in a dashboard.

## Later additions
Add cited question-answering over selected papers, evaluate retrieval quality, and add permitted AI-news sources.
## Data flow
1. Fetch recent AI paper metadata from arXiv.
2. Save each paper by its arXiv ID to avoid duplicates.
3. Search saved titles and abstracts.
4. Display results with links to the original papers.

## First success checks
- Refreshing twice does not create duplicate papers.
- A failed refresh does not remove saved papers.
- Search results include a title, publication date, and source link.
- The dashboard shows when it was last refreshed.

## Limits
The first version does not download PDFs, answer questions about full papers, or guarantee coverage of every AI paper or news item.

---

## Revision 2 (2026-09-28): cited question-answering over selected papers

The "later addition" of cited question-answering is now the main goal. The sections above
are kept as the original plan; this section records what changed and why.

### What changed
| Original plan | Revision 2 | Why |
|---|---|---|
| Fetch *recent* `cs.AI` papers | Search arXiv with a **user-entered query** | Users want papers on a specific topic, not just the newest ones. |
| No PDF downloads | Download PDFs **only for papers the user selects** | Question-answering needs full text; downloading only chosen papers keeps the collection small (still in the spirit of the original goal). |
| Search titles and abstracts | Search **chunks of full text** (or the abstract when full text is unavailable) | Answers need specific passages with page numbers. |
| Dashboard | **Command-line app** first | Simplest to build, test, and explain. A dashboard can reuse the same functions later. |

### Data flow
1. **Search** arXiv with the user's query (at most one request every 3 seconds, retries when arXiv is busy).
2. **Save** each result's metadata in SQLite, keyed by arXiv ID (no duplicates).
3. **Select** papers to ingest. For each: download the PDF, extract text page by page, clean it.
   If the PDF can't be downloaded or has no extractable text, the paper is labelled
   `abstract_only` and only its abstract is used.
4. **Chunk** the text into overlapping passages; each chunk keeps its paper ID and page number.
5. **Index** the chunks with SQLite's built-in full-text search (FTS5, BM25 ranking).
6. **Ask**: retrieve the best passages for a question and check there is enough evidence.
7. **Answer**: show the passages (retrieval-only mode, the default) or, if a local Ollama
   model is installed, a generated answer whose citations are checked against the passages.

### Success checks (in addition to the first ones)
- Every passage shown can be traced to a paper ID and a page number (or "abstract").
- Papers without usable full text are clearly labelled `abstract_only`.
- Questions the collection can't support get an explicit "not enough evidence" result.
- Closing and reopening the app keeps papers, downloaded PDFs, and the search index.
- A generated answer is only shown if every citation points to a retrieved passage.

### Limits
- Keyword search (BM25) can miss passages that use different words than the question.
- Text extraction from PDFs loses tables, equations, and figure contents.
- Citations are checked to exist, not proven to support each sentence; users should read the cited passage.
