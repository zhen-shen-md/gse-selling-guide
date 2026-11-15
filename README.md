# GSE Selling Guide

A searchable, versioned knowledge base of the **Fannie Mae Selling Guide** and the selling scope of the
**Freddie Mac Single-Family Seller/Servicer Guide**, with:

- **Search** across both guides by topic, phrase, or section number.
- **Ask & compare**: one plain-language question returns the most relevant sections from each guide side by side.
- **Side-by-side reading** of a section and its likely counterparts in the other guide.
- **What's new**: every section-level change between editions, linked to the announcement or bulletin that introduced it, including Freddie Mac changes that are published but not yet effective.
- **Pricing**: Fannie Mae LLPAs and Freddie Mac credit fees compared cell by cell, parsed from each agency's PDF.

Every result links back to the official source. Optional AI summaries are available when you run the
server with an API key; everything else works without AI, including the static website.

> **Not official guidance.** This is an independent research tool, not affiliated with Fannie Mae, Freddie Mac,
> or FHFA. Read [NOTICE.md](NOTICE.md).

**Live site:** https://zhen-shen-md.github.io/gse-selling-guide/ (published by GitHub Pages from `main`).

## Current contents

| Source | Current edition | Earlier editions kept for change history |
|---|---|---|
| Fannie Mae Selling Guide | November 4, 2026 — 427 topics | June 3, August 5, September 2, October 7, 2026 (monthly) |
| Freddie Mac Guide, Seller/Servicer Relationship + Selling segments | Bulletin 2026-15, November 4, 2026 — 532 current sections + 93 future-effective versions | Bulletin 2026-9, July 1, 2026 |
| Fannie Mae LLPA Matrix | September 30, 2026 | January 28, 2026 |
| Freddie Mac Exhibit 19, Credit Fees | November 4, 2026 (Bulletin 2026-15) | July 1, 2026 |
| Fannie Mae Selling Guide announcements | SEL-2026-06 through SEL-2026-10 | — |

**Changes between the editions above:**

| | Fannie Mae, month by month | Freddie Mac (Jul 1 → Nov 4) |
|---|---|---|
| Substantive changes now in effect | 61: Jun→Aug 25, Aug→Sep 8, Sep→Oct 19, Oct→Nov 9 | 145 (86 revised, 39 took effect, 10 merged, 8 renumbered, …) |
| Upcoming (published, effective later) | — | 89 sections effective Nov 9, 2026 – Sep 27, 2027 |

Freddie Mac publishes only its latest compiled Guide PDF, so its earlier monthly editions cannot be downloaded;
each Freddie change still carries its own revision or effective date.

**Pricing:** all 441 like-for-like percentage cells are equal across the two agencies. Freddie Mac removed its
Alt FICO fee rows in November 2026, and Fannie Mae's grids now apply to Classic FICO or VantageScore 4.0.

## Using the data

Everything under `knowledge_base/` is plain JSON/JSONL, so you can load it into your own tools or a vector database.

```
knowledge_base/
  editions/<fannie|freddie>/<YYYY-MM-DD>/
    sections.jsonl     one record per guide section (Freddie: one per current or future-effective version)
    chunks.jsonl       retrieval-sized chunks (≤4,500 chars, 500 overlap); latest edition only
    outline.json       full PDF bookmark tree
    manifest.json      source fingerprint, scope, counts
    validation.json    extraction checks
  changes/<agency>/<old>__<new>.jsonl     section-level change records per consecutive edition pair (+ .summary.json)
  crosswalk/crosswalk_candidates.jsonl    top-5 Freddie candidates per Fannie topic
  crosswalk/curated_pairs.json            human-reviewed pairs (override machine candidates)
  pricing/                                parsed grid cells per document, comparisons, version changes
  official_links.json                     verified links to Fannie's online guide pages
  catalog.json, validation_summary.json   registry and cross-checks
```

Key fields in `sections.jsonl`: `section_id`, `section_title`, `version_status` (`current` or `future`),
`revision_date`, `effective_date`, `hierarchy`, `pdf_page_start`, `citation`, `source_url` (edition-pinned PDF
page, Fannie), `official_url` (Freddie section page), `text`, `text_sha256`.

Change records carry `change_type` (`revised`, `added`, `removed`, `renumbered`, `retitled`, `merged`, `split`,
`restructured`, `took_effect`, `upcoming`, `upcoming_revised`, `editorial`, `layout_only`), old and new IDs,
titles and dates, the linked announcement, added and removed sentences, and successor sections for merges and splits.

### Command line

```bash
python pipeline/query.py search "verbal verification of employment"
python pipeline/query.py search "B3-3.8-02" --agency fannie
python pipeline/query.py search "cash-out refinance" --agency freddie --include-future
python pipeline/query.py compare B2-1.3-03
python pipeline/query.py changes --agency fannie --type revised
python pipeline/query.py pricing --transaction purchase --score 745 --ltv 78 --attributes condominium
```

(`search` uses the local SQLite index that the builders create; run the build once after cloning.)

## Running it locally

Requirements: Python 3.11+, Node 20+.

```bash
python -m venv .venv && .venv/Scripts/activate      # or: source .venv/bin/activate
pip install -r requirements.txt
python pipeline/fetch_sources.py                     # download official PDFs and verify hashes
python pipeline/build_all.py --rebuild-editions --rebuild-changes   # rebuild everything (~15 minutes)
npm start                                            # http://127.0.0.1:4173
```

To browse the committed data without downloading any PDFs, run only `python pipeline/build_site_data.py`,
then `npm start`.

**Optional AI.** Set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`) before `npm start`. This adds AI summaries
to *Ask & compare* and an *Explain with AI* button in *What's new*. The key stays on the server. Answers are
constrained to the retrieved text and fall back to the evidence-only summary if the call fails. The static
GitHub Pages site always runs in evidence-only mode.

## How it works

Full technical specification (chunk design, crosswalk scoring, change detection, pricing parser): [docs/TECHNICAL.md](docs/TECHNICAL.md).

1. **Extraction.** Each PDF is split at its canonical section headings, located through the PDF bookmarks.
   Freddie's Servicing segment boundary is read from the bookmarks, never hardcoded, because it moves between
   editions (page 1,966 in July, page 2,103 in November). Malformed bookmarks, such as `(11/04/026)` or a
   future version missing its "Future effective date" label, are repaired and logged as warnings.
2. **Change detection.** Sections are matched across editions by ID and title, then title alone (renumbering),
   then ID with a similar title, then text similarity. Removed sections are traced to the sections that absorbed
   their text. Word-order-only differences are marked `layout_only`. Text changes without a new revision date are
   marked `editorial`.
3. **Crosswalk.** A deterministic hybrid matcher (BM25 over titles and text, title overlap, mortgage-domain
   aliases) proposes counterparts. Pairs in `curated_pairs.json` are treated as ground truth and test cases.
4. **Pricing.** Grid cells are parsed from each agency's PDF text, never typed in by hand, then compared cell by
   cell. Credits and waivers are a short transcribed list that every build re-verifies against the PDF text.
5. **Validation.** `pipeline/validate_knowledge_bases.py` cross-checks every artifact and runs in CI.

## Evaluation

A 120-question retrieval benchmark ([eval/](eval/README.md)) compares the production keyword search, BM25, local
embeddings, and hybrids. Hybrid keyword + embedding retrieval raises hit@5 from 0.81 to 0.89 with no regressions
(plain-language questions: 0.56 → 0.73). Embedding-only search is worse on identifiers. See
[eval/results/REPORT.md](eval/results/REPORT.md).

## Known limitations

- The crosswalk is a relevance aid: 80 of 427 top matches are low-confidence. Help by reviewing pairs (see below).
- Production search is still keyword-only; the evaluation shows a hybrid with embeddings is better (not yet shipped).
- External documents incorporated by reference (forms, Exhibit 19A, the Eligibility Matrix) are not indexed as text.
- About 7% of Fannie sections have no verified online-guide link and fall back to the official PDF page.
- Freddie Mac bulletins are not yet parsed. Freddie changes are dated from the Guide itself, and announcement links cover Fannie only.

## Updating to a new edition

A scheduled workflow (`check-new-editions.yml`) opens an issue when either agency publishes something new. To update:

1. Add the new document to `sources/manifest.json` (URL, edition date, SHA-256; `fetch_sources.py` prints the hash),
   and add new announcements.
2. `python pipeline/fetch_sources.py && python pipeline/build_all.py --links`
3. Review `knowledge_base/validation_summary.json`, the change summaries, and any builder warnings. Check that
   `crosswalk/validation.json` reports no stale curated pairs.
4. Commit and open a PR. CI validates the data; merging to `main` redeploys the site.

**Retention:** only the latest edition per agency keeps `chunks.jsonl` (the retrieval layer). Earlier editions keep
`sections.jsonl`, `outline.json`, and their manifests (about 3 MB per Fannie edition), which is all that change
detection needs. `build_all.py` applies this automatically.

## Contributing

The most valuable contribution is **reviewing crosswalk pairs**. Add or confirm entries in
`knowledge_base/crosswalk/curated_pairs.json` with `relationship` (`counterpart`, `partial`, `no_counterpart`),
`status: "reviewed"`, `reviewed_by`, and `reviewed_on`. Low-confidence candidates waiting for review are listed
in `crosswalk/review_queue.jsonl`.

Run `python -m unittest discover -s tests` and `npm test` before opening a PR.

## License

Code: MIT ([LICENSE](LICENSE)). Guide and pricing content belongs to Fannie Mae and Freddie Mac ([NOTICE.md](NOTICE.md)).
