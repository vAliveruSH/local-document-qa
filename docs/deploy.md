# Public read-only demo

A separate, read-only version of the app that friends can open in a browser. It answers questions
about a small, fixed set of openly licensed papers. The full app (search, add, ingest, remove,
optional local model) stays on your own computer and is never exposed.

> **Status:** code and tests are done, and the demo corpus is included in the project's `demo/`
> folder (licence and attribution in [`demo/README.md`](../demo/README.md)). The demo has not been
> deployed. It counts as working only after a friend has opened the live link and the checks in
> [section 5](#5-verify-the-live-link) pass.

## 1. What the demo can and cannot do

| | Full local app (`python app.py serve`) | Public demo (`docqa/demo.py`) |
|---|---|---|
| Search arXiv | Yes | No: there is no search route |
| Add, ingest, retry, remove papers | Yes | No: those routes don't exist (404/405) |
| Ask questions, cited passages | Yes | Yes, over the bundled papers only |
| Generated answers (Ollama) | Optional | No: no model is created; `generate` is rejected (422) |
| Database | `data/library.db`, read and write | `demo/library.db`, opened read-only, writes refused |
| Network calls at runtime | arXiv, optionally Ollama | None |

Other protections:
- **Limits:** questions of at most 500 characters, at most 10 passages per answer, and 20 questions
  per minute per visitor (300 per server instance).
- **No API docs pages:** `/docs`, `/redoc` and `/openapi.json` are turned off.
- **Headers:** security headers (content security policy, no framing, no referrer) are sent.
- **Errors:** generic error messages that never include server paths.
- **Refuses to start serving** (503) if `demo/corpus.json` and `demo/library.db` list different
  papers or versions, if any licence is not CC BY 4.0 or CC0 1.0, or if the database still contains
  local file paths.

## 2. The bundled papers and their licences

Only papers whose **exact version** is licensed CC BY 4.0 (or CC0) are bundled, because that
licence allows sharing the text with attribution. Most arXiv papers use arXiv's non-exclusive
distribution licence, which does not allow republishing, so they are not eligible.

| Paper | Version | Licence (shown on that version's arXiv page) |
|---|---|---|
| Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection (Asai, Wu, Wang, Sil, Hajishirzi) | [2310.11511v1](https://arxiv.org/abs/2310.11511v1) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Towards Universal Dense Retrieval for Open-domain Question Answering (Sciavolino) | [2109.11085v1](https://arxiv.org/abs/2109.11085v1) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| W-RAG: Weakly Supervised Dense Retrieval in RAG for Open-domain Question Answering (Nian, Peng, Wang, Fang) | [2408.08444v2](https://arxiv.org/abs/2408.08444v2) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Parametric Retrieval Augmented Generation (Su, Tang, Ai, Yan, Wang, Wang, Ye, Zhou, Liu) | [2501.15915v1](https://arxiv.org/abs/2501.15915v1) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

**Attribution** shown on every paper card and every passage: title, authors, arXiv ID and version
(linked), licence (linked), and the change note: *"Text extracted from the PDF and split into
passages; figures, tables and equations are omitted or altered; author email addresses removed."*

**`demo/library.db` contains:**
- arXiv metadata of these versions (CC0);
- the extracted text as passages with page numbers, plus its search index.

**It does not contain:** PDFs, local file paths, cached search results, or search history.

## 3. Build the corpus (on your computer)

```powershell
python scripts/build_demo_corpus.py
```

The script:
1. **Checks each licence.** It reads each exact version's arXiv page and **stops** unless it shows
   the expected licence.
2. **Downloads and indexes.** It fetches that version's metadata, downloads the PDF to a temporary
   folder outside the project, and indexes the text with the same code as the local app. It stops
   unless the full text was extracted.
3. **Writes the two demo files:** `demo/library.db` (bundled, read-only safe) and
   `demo/corpus.json` (licence and attribution).
4. **Checks the result.** It runs the same checks the demo runs, then deletes the temporary folder,
   including the PDFs.

Preview locally, then open http://127.0.0.1:8001:

```powershell
python -m uvicorn docqa.demo:app --port 8001
```

## 4. Deploy on Vercel (Hobby plan)

The Hobby plan is free and for **non-commercial** use. If usage goes over its limits, Vercel pauses
the project until the 30-day window resets instead of charging. Vercel's documentation does not
explicitly say whether a card is required at sign-up; check this yourself when you create the account.

How the project is set up for Vercel:

| File | Purpose |
|---|---|
| `pyproject.toml` | `[tool.vercel] entrypoint = "docqa.demo:app"` (otherwise Vercel would pick `app.py`, the command-line launcher) |
| `vercel.json` | Includes `demo/**` and `docqa/web/**` in the function; excludes `tests/`, `data/`, `evaluation/`, `docs/` |
| `.python-version` | `3.12` (Vercel's default Python version) |
| `requirements.txt` | Runtime dependencies |

Steps (you do these; nothing here is automated):
1. Build the corpus (section 3), review `demo/corpus.json`, and commit `demo/`.
2. Push to GitHub.
3. Create a Vercel account and import the GitHub repository. Keep the default settings.
4. Open the deployment URL and run the checks below.

**Not verified yet:** that Vercel honours `includeFiles` as configured; that it installs from
`requirements.txt` when `pyproject.toml` has only a `[tool.vercel]` table; and how long a cold start
takes. The first deployment answers all three.

## 5. Verify the live link

- [ ] The page opens on **Ask**, with the demo banner and no Discover link.
- [ ] **Collection** lists exactly the four papers, each with its licence line, and no Remove button.
- [ ] Asking *"What are reflection tokens in Self-RAG?"* returns passages with page numbers and licence lines.
- [ ] A question the papers don't cover (for example *"What is the boiling point of water?"*) says there is not enough information.
- [ ] `https://<your-link>/docs` returns 404.
- [ ] A friend on another network opens the link and gets an answer.

## 6. Continuous testing

`.github/workflows/tests.yml` runs the full test suite on Linux with Python 3.11 and 3.12 on every
push and pull request. GitHub Actions is free for public repositories.
