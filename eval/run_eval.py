#!/usr/bin/env python3
"""Retrieval and crosswalk evaluation: keyword vs BM25 vs embeddings vs hybrid.

Usage:
  pip install -r eval/requirements.txt
  node eval/run_keyword.mjs
  python eval/run_eval.py

Writes eval/results/metrics.json and eval/results/REPORT.md. Embeddings are cached in eval/.cache/.

Retrieval task: for each question and agency, rank the latest edition's current sections; a hit is any
expected section. Crosswalk task: for each Fannie topic in the gold set, rank Freddie sections; a hit is any
gold counterpart. Gold = curated pairs + pairs implied by the question set (see eval/README.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
from kb_common import KB, read_json, read_jsonl, split_chunks  # noqa: E402

EVAL = ROOT / "eval"
CACHE = EVAL / ".cache"
RESULTS = EVAL / "results"
MODEL = "BAAI/bge-small-en-v1.5"
RRF_K = 60
STOP = set("""a an and are as at be by can do does for from has have how i if in into is it its may me my of on or
our so that the their them they this to up use used using what when where which who will with without would we you your
loan loans mortgage mortgages borrower borrowers""".split())


def latest(agency: str) -> Path:
    return sorted(p for p in (KB / "editions" / agency).iterdir() if p.is_dir())[-1]


def header(rec: dict) -> str:
    h = " > ".join(v for v in (rec.get("hierarchy") or {}).values() if v)
    return f"{rec['agency']} {rec['section_id']} {rec['section_title']}\n{h}\n"


# ---------------- retrievers ----------------

def bm25_rankings(agency: str, queries: dict[str, str]) -> dict[str, list[str]]:
    """SQLite FTS5 BM25 over chunks (the CLI engine), OR-query, best chunk per section."""
    con = sqlite3.connect(latest(agency) / "search.sqlite")
    out = {}
    for qid, text in queries.items():
        terms = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP and len(t) > 1]
        if not terms:
            out[qid] = []
            continue
        rows = con.execute(
            """SELECT c.section_id FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
               WHERE chunks_fts MATCH ? AND c.version_status = 'current'
               ORDER BY bm25(chunks_fts, 0, 8, 4, 1, 1) LIMIT 300""",
            (" OR ".join(f'"{t}"' for t in dict.fromkeys(terms)),)).fetchall()
        ranking = []
        for (sid,) in rows:
            if sid not in ranking:
                ranking.append(sid)
        out[qid] = ranking[:50]
    return out


class Embedder:
    def __init__(self) -> None:
        from fastembed import TextEmbedding
        self.model = TextEmbedding(MODEL)

    def passages(self, name: str, texts: list[str]) -> np.ndarray:
        key = hashlib.sha256(("\x00".join(texts) + MODEL).encode("utf-8")).hexdigest()[:16]
        path = CACHE / f"{name}-{key}.npy"
        if path.exists():
            return np.load(path)
        print(f"  embedding {len(texts)} passages for {name} ...", flush=True)
        vecs = np.array(list(self.model.passage_embed(texts, batch_size=32)), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        np.save(path, vecs)
        return vecs

    def queries(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self.model.query_embed(texts)), dtype=np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def build_units(agency: str, variant: str) -> tuple[list[str], list[str]]:
    """Return (texts, section_ids) for an embedding variant over current chunks of the latest edition."""
    chunks = [c for c in read_jsonl(latest(agency) / "chunks.jsonl") if c["version_status"] == "current"]
    texts, owners = [], []
    for c in chunks:
        if variant == "embed_raw":
            pieces, prefix = [c["text"]], ""
        elif variant == "embed_ctx":
            pieces, prefix = [c["text"]], header(c)
        else:  # embed_ctx_small: ~1,500-char pieces so the model (512-token window) sees all the text
            pieces, prefix = split_chunks(c["text"], 1500, 200), header(c)
        for p in pieces:
            texts.append(prefix + p)
            owners.append(c["section_id"])
    return texts, owners


def embed_rankings(emb: Embedder, agency: str, variant: str, queries: dict[str, str]) -> dict[str, list[str]]:
    texts, owners = build_units(agency, variant)
    pv = emb.passages(f"{agency}-{variant}", texts)
    qids = list(queries)
    qv = emb.queries([queries[q] for q in qids])
    sims = qv @ pv.T
    out = {}
    for i, qid in enumerate(qids):
        best: dict[str, float] = {}
        for j in np.argsort(-sims[i])[:400]:
            sid = owners[j]
            if sid not in best:
                best[sid] = float(sims[i, j])
        out[qid] = list(best)[:50]
    return out


def rrf(*rankings: list[str]) -> list[str]:
    score: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, sid in enumerate(ranking, start=1):
            score[sid] += 1.0 / (RRF_K + rank)
    return sorted(score, key=lambda s: -score[s])[:50]


# ---------------- metrics ----------------

def first_hit(ranking: list[str], expected: set[str]) -> int | None:
    for i, sid in enumerate(ranking, start=1):
        if sid in expected:
            return i
    return None


def summarize(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    hit = lambda k: sum(1 for r in ranks if r is not None and r <= k) / n
    mrr = sum(1.0 / r for r in ranks if r is not None and r <= 10) / n
    return {"n": n, "hit@1": round(hit(1), 3), "hit@3": round(hit(3), 3), "hit@5": round(hit(5), 3),
            "hit@10": round(hit(10), 3), "mrr@10": round(mrr, 3)}


# ---------------- crosswalk ----------------

def crosswalk_gold(questions: list[dict]) -> dict[str, dict]:
    gold: dict[str, dict] = {}
    for p in read_json(KB / "crosswalk" / "curated_pairs.json")["pairs"]:
        if p.get("relationship") != "no_counterpart":
            gold[p["fannie_section_id"]] = {"freddie": set(p["freddie_section_ids"]), "source": "curated"}
    for q in questions:
        if q["fannie_expected"] and q["freddie_expected"] and not q["needs_sme_review"] and q["type"] != "identifier":
            fid = q["fannie_expected"][0]
            entry = gold.setdefault(fid, {"freddie": set(), "source": "question_set"})
            entry["freddie"] |= set(q["freddie_expected"])
    return gold


def crosswalk_eval(emb: Embedder, gold: dict[str, dict]) -> dict:
    from build_guide_crosswalk import FANNIE_ID_RE, build_candidates  # lexical matcher used in production

    fannie = [s for s in read_jsonl(latest("fannie") / "sections.jsonl") if FANNIE_ID_RE.match(s["section_id"])]
    freddie = [s for s in read_jsonl(latest("freddie") / "sections.jsonl")
               if s["section_id"] != "GUIDE-NOTICE" and s["version_status"] == "current"]
    targets = [s for s in fannie if s["section_id"] in gold]
    lexical: dict[str, list[str]] = defaultdict(list)
    for row in sorted(build_candidates(targets, freddie, 50), key=lambda r: r["rank"]):
        lexical[row["fannie"]["section_id"]].append(row["freddie"]["section_id"])

    card = lambda s: header(s) + s["text"][:1500]
    fv = emb.passages("xwalk-fannie-cards", [card(s) for s in targets])
    rv = emb.passages("xwalk-freddie-cards", [card(s) for s in freddie])
    sims = fv @ rv.T
    embedding = {s["section_id"]: [freddie[j]["section_id"] for j in np.argsort(-sims[i])[:50]]
                 for i, s in enumerate(targets)}
    methods = {"lexical (production)": lexical, "embedding": embedding,
               "hybrid (RRF)": {fid: rrf(lexical[fid], embedding[fid]) for fid in embedding}}
    result = {}
    for name, rankings in methods.items():
        ranks = [first_hit(rankings[fid], gold[fid]["freddie"]) for fid in gold if fid in rankings]
        result[name] = summarize(ranks)
    misses = []
    for fid in gold:
        if fid not in lexical:
            continue
        lr, er = first_hit(lexical[fid], gold[fid]["freddie"]), first_hit(embedding[fid], gold[fid]["freddie"])
        if (lr or 99) > 5 or (er or 99) > 5:
            misses.append({"fannie": fid, "gold": sorted(gold[fid]["freddie"]), "lexical_rank": lr, "embedding_rank": er,
                           "lexical_top": lexical[fid][:3], "embedding_top": embedding[fid][:3]})
    return {"gold_size": len(gold), "gold_sources": {k: sum(1 for g in gold.values() if g["source"] == k)
                                                     for k in ("curated", "question_set")},
            "metrics": result, "misses_at_5": misses}


# ---------------- main ----------------

def main() -> int:
    questions = read_jsonl(EVAL / "questions.jsonl")
    kw = read_json(CACHE / "keyword_rankings.json")
    emb = Embedder()
    rankings: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(dict)
    for agency in ("fannie", "freddie"):
        queries = {q["id"]: kw[q["id"]][agency]["query"] for q in questions}  # same agency-term mapping for all
        rankings["keyword (production)"][agency] = {qid: kw[qid][agency]["ranking"] for qid in queries}
        rankings["bm25 (FTS5)"][agency] = bm25_rankings(agency, queries)
        for variant in ("embed_raw", "embed_ctx", "embed_ctx_small"):
            rankings[variant][agency] = embed_rankings(emb, agency, variant, queries)
        rankings["hybrid keyword+embed"][agency] = {
            qid: rrf(rankings["keyword (production)"][agency][qid], rankings["embed_ctx_small"][agency][qid]) for qid in queries}
        rankings["hybrid bm25+embed"][agency] = {
            qid: rrf(rankings["bm25 (FTS5)"][agency][qid], rankings["embed_ctx_small"][agency][qid]) for qid in queries}

    metrics: dict = {}
    per_question: dict = defaultdict(dict)
    for method, by_agency in rankings.items():
        metrics[method] = {}
        pooled: dict[str, list] = defaultdict(list)
        for agency, ranks_by_q in by_agency.items():
            key = f"{agency}_expected"
            judged = [q for q in questions if q[key]]
            ranks = []
            for q in judged:
                r = first_hit(ranks_by_q[q["id"]], set(q[key]))
                ranks.append(r)
                pooled["all"].append(r)
                pooled[q["type"]].append(r)
                per_question[q["id"]][f"{method}|{agency}"] = r
            metrics[method][agency] = summarize(ranks)
        for kind, ranks in pooled.items():
            metrics[method][f"pooled_{kind}"] = summarize(ranks)

    xwalk = crosswalk_eval(emb, crosswalk_gold(questions))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "metrics.json").write_text(json.dumps({
        "model": MODEL, "questions": len(questions), "retrieval": metrics, "crosswalk": xwalk,
        "per_question_first_hit_rank": per_question}, indent=2), encoding="utf-8")
    write_report(questions, metrics, xwalk, per_question)
    print((RESULTS / "REPORT.md").read_text(encoding="utf-8"))
    return 0


def write_report(questions, metrics, xwalk, per_question) -> None:
    def table(cols: list[str]) -> str:
        head = "| Method | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols) + "\n"
        rows = []
        for method, m in metrics.items():
            cells = []
            for col in cols:
                scope, metric = col.split(" ")
                cells.append(f"{m[scope][metric]:.2f}")
            rows.append(f"| {method} | " + " | ".join(cells) + " |")
        return head + "\n".join(rows) + "\n"

    by_type = {k: sum(1 for q in questions if q["type"] == k) for k in ("terms", "plain", "identifier")}
    lines = [
        "# Retrieval evaluation report", "",
        f"Model for embeddings: `{MODEL}` (local, 384-d). Questions: {len(questions)} "
        f"({by_type['terms']} guide-wording, {by_type['plain']} plain-language, {by_type['identifier']} identifier). "
        "Each question is scored separately against Fannie and Freddie; pooled = both agencies together.", "",
        "## Overall (pooled across agencies)", "",
        table(["pooled_all hit@1", "pooled_all hit@5", "pooled_all hit@10", "pooled_all mrr@10"]),
        "## By question type (hit@5, pooled)", "",
        table(["pooled_terms hit@5", "pooled_plain hit@5", "pooled_identifier hit@5"]),
        "## By agency (hit@5 / MRR@10)", "",
        table(["fannie hit@5", "fannie mrr@10", "freddie hit@5", "freddie mrr@10"]),
        "## Crosswalk (Fannie topic -> Freddie counterpart)", "",
        f"Gold set: {xwalk['gold_size']} Fannie topics "
        f"({xwalk['gold_sources']['curated']} curated pairs, {xwalk['gold_sources']['question_set']} derived from the question set).", "",
        "| Method | hit@1 | hit@3 | hit@5 | MRR@10 |", "|---|---|---|---|---|",
        *[f"| {k} | {v['hit@1']:.2f} | {v['hit@3']:.2f} | {v['hit@5']:.2f} | {v['mrr@10']:.2f} |" for k, v in xwalk["metrics"].items()],
        "", "Method notes: *embed_raw* = chunk text only (the model reads only the first 512 tokens of each 4,500-char chunk); "
        "*embed_ctx* = same plus a header (agency, section ID, title, hierarchy); *embed_ctx_small* = header + ~1,500-char pieces "
        "so all text is embedded; hybrids use reciprocal rank fusion (k=60) with *embed_ctx_small*.", "",
        "Caveat: expected sections were drafted from section titles and need SME review (eval/README.md). "
        "Treat differences of a few points as noise on a set this size.",
    ]
    (RESULTS / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
