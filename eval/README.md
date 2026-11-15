# Evaluation

Measures how well the knowledge base finds the right guide sections, and whether embeddings help.
Results: [results/REPORT.md](results/REPORT.md) (full numbers and per-question ranks in `results/metrics.json`).

## Run it

```bash
pip install -r eval/requirements.txt     # fastembed (local ONNX embedding model, no API key)
python pipeline/build_site_data.py       # if web/public/data is missing
node eval/run_keyword.mjs                # production keyword retriever -> eval/.cache/
python eval/run_eval.py                  # BM25, embeddings, hybrids, crosswalk -> eval/results/
```

The first run downloads `BAAI/bge-small-en-v1.5` (about 130 MB) and embeds the latest editions. Later runs use
the cache in `eval/.cache/`.

## Question set (`questions.jsonl`)

There are 120 questions, generated from `questions_draft.py`. Each lists the sections that should answer it in each
guide; any listed section counts as a hit. The questions come in three types:

| Type | Count | Purpose | Example |
|---|---|---|---|
| `terms` | 74 | Uses the guides' own wording | "Interested party contribution limits" |
| `plain` | 33 | Everyday language that avoids the section-title words, to test the vocabulary gap | "The home seller wants to pay part of the buyer's closing costs. How much is allowed?" |
| `identifier` | 13 | Form numbers, program names, scores, section numbers | "IRS Form 4506-C", "VantageScore 4.0" |

**How the expected sections were chosen.** They were picked from the two guides' section-title lists (tables of
contents) *before* any retriever was run, so the test set does not encode any method's behaviour.

**Known weaknesses.**
- The set was drafted by one author, with AI assistance.
- 11 items are flagged `needs_sme_review` because the right Freddie section is uncertain.
- Before quoting results externally, have a policy subject-matter expert confirm or correct the expected sections.
  Adding questions taken from real user logs or each month's announcements would make the set more representative.

## Methods compared

| Method | What it is |
|---|---|
| keyword (production) | The website's scorer in `web/public/core.mjs`, run unmodified through Node |
| bm25 (FTS5) | SQLite FTS5 BM25 over chunks (the CLI engine), OR-query, best chunk per section |
| embed_raw | `bge-small-en-v1.5` over each chunk's text. The model reads only the first 512 tokens of each 4,500-character chunk |
| embed_ctx | The same, with a header prepended: agency, section ID, title, hierarchy |
| embed_ctx_small | Header plus pieces of about 1,500 characters, so the whole text is embedded; best piece per section |
| hybrid keyword+embed / hybrid bm25+embed | Reciprocal rank fusion (k = 60) of a lexical ranking with `embed_ctx_small` |

Rules that apply to every method:
- Every method receives the same query, after the agency terminology mapping (limited cash-out ↔ no cash-out).
- Every method ranks only the current sections of the latest edition.

## Metrics

- **hit@k:** share of questions with at least one expected section in the top k.
- **MRR@10:** mean of 1/rank of the first expected section, counting 0 when it is not in the top 10.

Each question is scored once per agency that has expected sections. "Pooled" combines both agencies, about 238
judgements in total.

## Crosswalk evaluation

**Gold set.** It combines two sources:
- the curated pairs in `knowledge_base/crosswalk/curated_pairs.json`;
- one pair per Fannie topic implied by the question set: the first expected Fannie section, matched to all
  expected Freddie sections. Flagged items and identifier questions are excluded.

**Methods compared:**
- the production lexical matcher (`build_guide_crosswalk.py`);
- embedding similarity of section "cards" (header plus the first 1,500 characters);
- reciprocal rank fusion of the two.
