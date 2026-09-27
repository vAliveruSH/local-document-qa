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