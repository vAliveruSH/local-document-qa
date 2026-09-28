# local-document-qa

A local, command-line research assistant for arXiv papers. You search arXiv, pick papers,
and the app downloads and indexes their full text on your computer. Then you ask
questions and get the most relevant passages, each cited to a paper and page number.
When the library doesn't contain enough evidence, the app says so instead of guessing.

- Runs on Windows with Python 3.11. No paid API, no account, no cloud service.
- Everything is stored locally and kept between runs (see [What is stored locally](#what-is-stored-locally)).
- **Default mode is retrieval-only.** The app shows passages from the papers; it does not write
  an answer. An optional mode uses a free local model through [Ollama](https://ollama.com) to write
  a cited answer. That mode is implemented and unit-tested with a fake model, but it has **not been
  run against a real Ollama model yet**.

## Setup (Windows PowerShell)

```powershell
git clone https://github.com/vAliveruSH/local-document-qa.git
cd local-document-qa
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

If `Activate.ps1` is blocked ("running scripts is disabled"), either run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation and use
`.\.venv\Scripts\python.exe` wherever this README says `python`.

## Usage

```powershell
python app.py search "retrieval augmented generation"   # search arXiv, save metadata
python app.py search "attention" --sort recent --max 20  # newest first, 20 results
python app.py search "attention" --start 10              # next page of results
python app.py add 1706.03762 1810.04805                  # select papers: download + index
python app.py list                                       # what's in your library
python app.py ask "What percentage of tokens does BERT mask?"
python app.py ask "..." --paper 1810.04805               # search one paper only
python app.py ask "..." --top 3                          # show fewer passages
python app.py ask "..." --generate                       # optional: local model writes a cited answer
python app.py --help                                     # all commands and options
```

Search accepts plain words (all must appear), `"quoted phrases"`, or arXiv's own syntax such as
`ti:transformer AND cat:cs.CL`. `add` accepts IDs like `1706.03762`, `arXiv:1706.03762v7`,
or an arxiv.org link.

## Example session

Real output from 2026-09-28, trimmed where marked `...`.

```text
PS> python app.py search "retrieval augmented generation" --max 3
Showing results 1-3 of about 8,516 for: retrieval augmented generation

[1] AR-RAG: Autoregressive Retrieval Augmentation for Image Generation
    ID: 2506.06962v3   (new)
    Authors: Jingyuan Qi, Zhiyang Xu, Qifan Wang et al. (4 authors)
    Published: 2025-06-08   Updated: 2025-06-14   Category: cs.CV
    Link: https://arxiv.org/abs/2506.06962v3
    We introduce Autoregressive Retrieval Augmentation (AR-RAG), a novel paradigm ...
...
Saved metadata: 3 new, 0 already in your library.

PS> python app.py add 1706.03762 1810.04805 2005.11401 not-an-id
INVALID ID       not-an-id
                 'not-an-id' does not look like an arXiv ID (expected something like 1706.03762).
FULL TEXT        1706.03762 Attention Is All You Need
                 15 pages; 54 searchable passage(s)
FULL TEXT        1810.04805 BERT: Pre-training of Deep Bidirectional Transformers for Language...
                 16 pages; 87 searchable passage(s)
FULL TEXT        2005.11401 Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks
                 19 pages; 87 searchable passage(s)

Summary: 3 full text, 1 invalid id.

PS> python app.py ask "What percentage of tokens does BERT mask during pre-training?" --top 2
Question: What percentage of tokens does BERT mask during pre-training?
Key words searched: percentage, tokens, bert, mask, pre, training

RETRIEVAL-ONLY RESULT - no answer was generated. These are the most relevant passages
from your library; read them to find the answer.

[1] BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding
    arXiv:1810.04805v2, page 4   score 10.87   matched: percentage, tokens, mask, pre, training
    https://arxiv.org/pdf/1810.04805v2#page=4
    ... In all of our experiments, we mask 15% of all WordPiece tokens in each sequence at random. ...
...

PS> python app.py ask "What is the capital of France?"
NOT ENOUGH EVIDENCE - no answer given.
  No passage in your library contains any of the words: capital, france.
```

Closing PowerShell and running `ask` again works immediately: nothing is downloaded or re-indexed.

## How it works

```
 search ──► arXiv API ──► parse Atom XML ──► SQLite: papers (key = arXiv ID)
 add    ──► download PDF ──► text per page ──► clean ──► 150-word chunks (+page no.)
                 │ fails / no text                         │
                 └──► abstract_only (abstract is indexed)  └──► SQLite FTS5 index
 ask    ──► keywords ──► BM25 top passages ──► enough evidence?
                                                   ├─ no  ──► "NOT ENOUGH EVIDENCE"
                                                   └─ yes ──► retrieval-only passages
                                                              or (optional) Ollama answer,
                                                              kept only if its [n] citations are real
```

| File | Job |
|---|---|
| [app.py](app.py) | Entry point; runs the command-line interface |
| [docqa/arxiv_client.py](docqa/arxiv_client.py) | Builds queries, waits ≥3 s between requests, retries, parses metadata |
| [docqa/storage.py](docqa/storage.py) | SQLite tables for papers, chunks, and the full-text index |
| [docqa/pdf_text.py](docqa/pdf_text.py) | Extracts and cleans text page by page (pypdf) |
| [docqa/chunking.py](docqa/chunking.py) | Splits pages into overlapping chunks that keep their page number |
| [docqa/ingest.py](docqa/ingest.py) | Download → extract → chunk → index, or fall back to abstract-only |
| [docqa/retrieval.py](docqa/retrieval.py) | BM25 search and the "enough evidence" check |
| [docqa/answer.py](docqa/answer.py) | Chooses retrieval-only / generated / insufficient-evidence; checks citations |
| [docqa/local_llm.py](docqa/local_llm.py) | Optional Ollama client |
| [docqa/workflow.py](docqa/workflow.py) | The search and add actions, separate from printing |
| [docqa/cli.py](docqa/cli.py) | Commands and output formatting |

### Design choices

- **SQLite instead of a separate database server:** it comes with Python, is a single file, and
  its transactions make re-ingesting safe (old chunks are replaced, never duplicated).
- **Keyword search (BM25 via SQLite FTS5) instead of embeddings:** there's nothing extra to
  install, it's fast, and the index is stored in the same file. Results are also explainable:
  the app shows which question words each passage matched. The cost is that it can't match
  synonyms (see Limitations). Embeddings would need a ~1 GB PyTorch install.
- **Chunks never cross pages:** every passage has exactly one page number to cite. Chunks are
  150 words with a 30-word overlap, so a sentence cut at one edge appears whole in the neighbour.
- **"Enough evidence" rule:** the best passage must contain at least 60% of the question's
  key words (stopwords removed). It is simple and imperfect; the evaluation below shows where it fails.
- **Generation is optional and checked:** a generated answer is discarded if it cites no
  passage or cites a passage number that wasn't retrieved.
- **Polite to arXiv:** at most one request every 3 seconds, a descriptive User-Agent, and
  retries with growing waits (3 s, 6 s, 12 s). During development arXiv intermittently
  answered valid requests with HTTP 406, so 406 is treated as "retry later".

## What is stored locally

Everything goes in the `data/` folder, which Git ignores:

| Path | Contents |
|---|---|
| `data/library.db` | SQLite database: paper metadata, ingest status and reason, text chunks, search index, time of last search |
| `data/pdfs/` | Downloaded PDFs, named like `1706.03762v7.pdf` (reused instead of downloading again) |
| `data/eval/` | A separate library used only by the evaluation script |

Delete `data/` to start fresh. Set the `DOCQA_DATA_DIR` environment variable to use another folder.

## Optional: generated answers with Ollama

Not tested with a real model yet: only the prompt, the citation checks and the error handling
are tested (with a fake model).

1. Install Ollama from https://ollama.com (free).
2. `ollama pull llama3.2:3b` (about 2 GB).
3. `python app.py ask "your question" --generate` (use `--model <name>` for another model).

If Ollama isn't running, the app prints how to fix it and falls back to retrieval-only output.
Answers are marked `GENERATED ANSWER` and list the cited passages. The app checks that
citations point to retrieved passages, but it can't prove each sentence is supported, so read the passages.

## Tests

```powershell
python -m pytest
```

There are 97 tests, all passing. They cover metadata parsing (using real saved arXiv responses),
query building, retries, timeouts, duplicate prevention, keeping saved papers when a search
fails, PDF extraction and failures, chunk page tracking, retrieval ranking, insufficient
evidence, and citation checking. The tests never use the network: HTTP calls are faked and
the test PDFs are generated in memory.

## Evaluation

```powershell
python -m evaluation.run_eval --setup   # first time: ingests 3 papers into data/eval/
python -m evaluation.run_eval           # rerun and rewrite evaluation/results.md
```

[evaluation/questions.json](evaluation/questions.json) has 25 questions about 3 papers
(Transformer, BERT, RAG):
- **19 answerable questions.** Each has the page and an exact evidence snippet, checked by the script
  against the ingested text.
- **6 questions the papers can't answer.**

Results from 2026-09-28 (full table in [evaluation/results.md](evaluation/results.md)):

| Metric | Result |
|---|---|
| Correct passage ranked 1st (hit@1) | 13/19 (68%) |
| Correct passage in top 5 (hit@5) | 17/19 (89%) |
| Mean reciprocal rank | 0.79 |
| Answerable questions judged "enough evidence" | 19/19 |
| Unanswerable questions correctly refused | 4/6 |

**What failed and why:**
- **B3** ("Which corpora..."): "corpora" doesn't match "corpus" because the stemmer doesn't know irregular plurals.
- **R1** ("generator ... parameters"): appendix pages mention those words more often and outrank page 3, which has the answer.
- **U3** (RLHF reward model) and **U4** (GPT-4 GPUs) were wrongly accepted. They share common
  words ("model", "fine", "tuning", "train", "gpt") with the papers.

I also tried weighting key words by rarity (IDF). With a 0.6 threshold it refused U3 but
wrongly refused an answerable question instead (the filler word "kind" got a high weight).
Both rules make 2 errors out of 25, so the simpler rule was kept. With only 25 questions,
these numbers are rough indicators, not a benchmark.

## Limitations

- Keyword search misses synonyms and paraphrases ("car" vs "automobile", "corpora" vs "corpus").
- The evidence check counts matching words; it doesn't understand meaning. It can accept questions
  on related topics the papers don't answer (see U3/U4 above).
- PDF text extraction loses tables, equations and figure contents. Two-column layouts can mix
  lines from both columns. Rejoining line-break hyphens can merge real hyphenated terms
  (e.g. "RAG-Sequence" broken across a line becomes "RAGSequence").
- Abstract-only papers can only answer questions about their abstract. They are labelled everywhere.
- The 3-second wait between arXiv requests applies within one command. Separate commands run
  back to back rely on you typing slower than that.
- If arXiv publishes a newer version of a paper you ingested, `list` points it out; re-run
  `add <ID> --force` to index the new version.
- Generated answers (Ollama) are untested with a real model, and citation checks can't prove support.

## Project history

The project started as "AI Research Radar", a metadata-only arXiv browser
([docs/design.md](docs/design.md)). Revision 2 of the design adds full-text ingestion and cited
question answering; the original design is kept in the same file for reference.
