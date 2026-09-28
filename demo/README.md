# Demo corpus: papers and licences

`library.db` in this folder is the search library used by the public read-only demo
(`docqa/demo.py`). It contains text from the four papers below, each an exact arXiv version
licensed under the [Creative Commons Attribution 4.0 International licence (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).
The licence of each version is shown on its arXiv page, linked below.

| Paper | Authors | arXiv version | Licence |
|---|---|---|---|
| Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection | Akari Asai, Zeqiu Wu, Yizhong Wang, Avirup Sil, Hannaneh Hajishirzi | [arXiv:2310.11511v1](https://arxiv.org/abs/2310.11511v1) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Towards Universal Dense Retrieval for Open-domain Question Answering | Christopher Sciavolino | [arXiv:2109.11085v1](https://arxiv.org/abs/2109.11085v1) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| W-RAG: Weakly Supervised Dense Retrieval in RAG for Open-domain Question Answering | Jinming Nian, Zhiyuan Peng, Qifan Wang, Yi Fang | [arXiv:2408.08444v2](https://arxiv.org/abs/2408.08444v2) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Parametric Retrieval Augmented Generation | Weihang Su, Yichen Tang, Qingyao Ai, Junxi Yan, Changyue Wang, Hongning Wang, Ziyi Ye, Yujia Zhou, Yiqun Liu | [arXiv:2501.15915v1](https://arxiv.org/abs/2501.15915v1) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

## Changes made to the papers

The text was extracted from each version's PDF and split into passages of about 150 words, each
tagged with its page number. Figures, tables and equations are omitted or altered by the
extraction. Author email addresses were removed from the passages and replaced with
`[email removed]`. No PDFs are included.

## Other contents

- **`library.db`:** the passages, their search index, and each paper's arXiv metadata (title,
  authors, abstract, dates, links, categories), which arXiv provides under
  [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
- **`corpus.json`:** the same licence and attribution details in machine-readable form. The demo
  refuses to serve the library unless the two files list the same papers and versions.

Rebuild both files with `python scripts/build_demo_corpus.py` (see [`docs/deploy.md`](../docs/deploy.md)).
