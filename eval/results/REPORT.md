# Retrieval evaluation report

Model for embeddings: `BAAI/bge-small-en-v1.5` (local, 384-d). Questions: 120 (74 guide-wording, 33 plain-language, 13 identifier). Each question is scored separately against Fannie and Freddie; pooled = both agencies together.

## Overall (pooled across agencies)

| Method | pooled_all hit@1 | pooled_all hit@5 | pooled_all hit@10 | pooled_all mrr@10 |
|---|---|---|---|---|
| keyword (production) | 0.59 | 0.81 | 0.87 | 0.69 |
| bm25 (FTS5) | 0.59 | 0.83 | 0.88 | 0.69 |
| embed_raw | 0.69 | 0.85 | 0.91 | 0.76 |
| embed_ctx | 0.71 | 0.85 | 0.91 | 0.78 |
| embed_ctx_small | 0.66 | 0.85 | 0.92 | 0.75 |
| hybrid keyword+embed | 0.66 | 0.89 | 0.92 | 0.76 |
| hybrid bm25+embed | 0.70 | 0.89 | 0.93 | 0.78 |

## By question type (hit@5, pooled)

| Method | pooled_terms hit@5 | pooled_plain hit@5 | pooled_identifier hit@5 |
|---|---|---|---|
| keyword (production) | 0.89 | 0.56 | 0.96 |
| bm25 (FTS5) | 0.90 | 0.67 | 0.88 |
| embed_raw | 0.93 | 0.68 | 0.83 |
| embed_ctx | 0.93 | 0.71 | 0.79 |
| embed_ctx_small | 0.93 | 0.67 | 0.88 |
| hybrid keyword+embed | 0.95 | 0.73 | 0.96 |
| hybrid bm25+embed | 0.95 | 0.74 | 0.92 |

## By agency (hit@5 / MRR@10)

| Method | fannie hit@5 | fannie mrr@10 | freddie hit@5 | freddie mrr@10 |
|---|---|---|---|---|
| keyword (production) | 0.83 | 0.74 | 0.78 | 0.64 |
| bm25 (FTS5) | 0.88 | 0.77 | 0.78 | 0.61 |
| embed_raw | 0.89 | 0.81 | 0.81 | 0.70 |
| embed_ctx | 0.88 | 0.84 | 0.82 | 0.71 |
| embed_ctx_small | 0.91 | 0.82 | 0.80 | 0.68 |
| hybrid keyword+embed | 0.92 | 0.78 | 0.86 | 0.73 |
| hybrid bm25+embed | 0.92 | 0.82 | 0.87 | 0.73 |

## Crosswalk (Fannie topic -> Freddie counterpart)

Gold set: 85 Fannie topics (11 curated pairs, 74 derived from the question set).

| Method | hit@1 | hit@3 | hit@5 | MRR@10 |
|---|---|---|---|---|
| lexical (production) | 0.68 | 0.82 | 0.87 | 0.78 |
| embedding | 0.72 | 0.88 | 0.91 | 0.81 |
| hybrid (RRF) | 0.74 | 0.93 | 0.94 | 0.83 |

Method notes: *embed_raw* = chunk text only (the model reads only the first 512 tokens of each 4,500-char chunk); *embed_ctx* = same plus a header (agency, section ID, title, hierarchy); *embed_ctx_small* = header + ~1,500-char pieces so all text is embedded; hybrids use reciprocal rank fusion (k=60) with *embed_ctx_small*.

Caveat: expected sections were drafted from section titles and need SME review (eval/README.md). Treat differences of a few points as noise on a set this size.
