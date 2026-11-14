#!/usr/bin/env python3
"""Build deterministic Fannie-to-Freddie candidate crosswalks for guide comparison."""

from __future__ import annotations

import argparse
import collections
import difflib
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

KB = Path(__file__).resolve().parents[1] / "knowledge_base"


FANNIE_ID_RE = re.compile(r"^[A-E]\d?-\d(?:\.\d+)?-\d{2}$")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a", "about", "all", "an", "and", "applicable", "application", "as", "at", "be",
    "by", "chapter", "contains", "date", "for", "from", "general", "guide", "in",
    "including", "information", "introduction", "loan", "loans", "mae", "mac", "mortgage",
    "mortgages", "of", "on", "or", "other", "overview", "part", "policy", "related",
    "requirement", "requirements", "section", "seller", "selling", "servicer", "subpart",
    "the", "this", "to", "topic", "transaction", "transactions", "with",
}
TOKEN_ALIASES = {
    "borrowers": "borrower", "properties": "property", "appraisals": "appraisal",
    "reports": "report", "documents": "document", "accounts": "account", "trusts": "trust",
    "credits": "credit", "incomes": "income", "insurances": "insurance", "ratios": "ratio",
    "condominiums": "condominium", "cooperatives": "cooperative", "refinancing": "refinance",
    "refinances": "refinance", "refinanced": "refinance", "manufacturedhousing": "manufactured",
    "eligibility": "eligible", "ineligible": "eligible",
    "homestyle": "renovation", "choicerenovation": "renovation", "homeready": "affordable",
    "homepossible": "affordable", "du": "aus", "lpa": "aus", "hcltv": "helocltv",
    "htltv": "helocltv", "cltv": "totalltv", "tltv": "totalltv", "arms": "arm",
}
PHRASE_ALIASES = {
    "desktop underwriter": ["aus", "automated", "underwriting"],
    "loan product advisor": ["aus", "automated", "underwriting"],
    "factory built": ["manufactured", "home"],
    "manufactured housing": ["manufactured", "home"],
    "manufactured homes": ["manufactured", "home"],
    "limited cash out": ["noncashout", "refinance"],
    "no cash out": ["noncashout", "refinance"],
    "high balance": ["highbalance", "superconforming"],
    "super conforming": ["highbalance", "superconforming"],
    "home ready": ["affordable"],
    "home possible": ["affordable"],
    "accessory dwelling unit": ["adu"],
    "co op": ["cooperative"],
    "adjustable rate": ["arm"],
    "community land trust": ["communitylandtrust"],
    "inter vivos revocable trust": ["livingtrust"],
    "living trust": ["livingtrust"],
}
DOMAIN_RULES = {
    "adjustable_rate": ["adjustable rate", " arm", "arms"],
    "affordable_lending": ["homeready", "home ready", "home possible", "affordable"],
    "appraisal_valuation": ["appraisal", "appraiser", "valuation", "property value"],
    "assets_reserves": ["asset", "reserve", "depository", "funds to close", "down payment"],
    "borrower_eligibility": ["borrower eligibility", "non-u.s", "non us", "living trust", "inter vivos"],
    "closing_documents": ["security instrument", "note", "rider", "power of attorney", "legal document"],
    "condo_coop": ["condominium", "co-op", "cooperative", "project eligibility"],
    "contract_compliance": ["contract", "representation", "warrant", "indemnif", "sanction"],
    "credit": ["credit report", "credit score", "tradeline", "derogatory", "credit history"],
    "debt_dti": ["debt-to-income", "debt to income", "monthly debt", "liabilit"],
    "disaster": ["disaster"],
    "flood_insurance": ["flood insurance", "flood zone"],
    "government": ["fha", "va-", "va ", "usda", "rhs", "government"],
    "income_employment": ["income", "employment", "self-employed", "self employed"],
    "insurance": ["mortgage insurance", "property insurance", "title insurance", "liability insurance"],
    "loan_delivery": ["delivery", "pooling", "mbs", "securit", "whole loan"],
    "ltv": ["ltv", "cltv", "hcltv", "tltv", "htltv", "loan-to-value"],
    "manufactured_housing": ["manufactured", "factory-built", "factory built"],
    "quality_control": ["quality control", " qc ", "prefunding", "post-closing"],
    "refinance": ["refinance", "refinancing", "cash-out", "cash out"],
    "renovation_construction": ["renovation", "construction-to-permanent", "construction to permanent"],
    "rental_income": ["rental income", "lease income"],
    "title": ["title insurance", "title exception", "title impediment"],
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def normalized_string(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = value.replace("®", "").replace("™", "")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens(value: str) -> list[str]:
    normalized = normalized_string(value)
    result: list[str] = []
    for token in TOKEN_RE.findall(normalized):
        token = TOKEN_ALIASES.get(token, token)
        if token not in STOPWORDS and len(token) > 1:
            result.append(token)
    for phrase, additions in PHRASE_ALIASES.items():
        if phrase in normalized:
            result.extend(additions)
    return result


def domains(value: str) -> list[str]:
    normalized = f" {normalized_string(value)} "
    return sorted(
        domain
        for domain, patterns in DOMAIN_RULES.items()
        if any(pattern in normalized for pattern in patterns)
    )


class BM25Index:
    def __init__(self, documents: list[list[str]]) -> None:
        self.size = len(documents)
        self.lengths = [len(document) for document in documents]
        self.average_length = sum(self.lengths) / max(1, self.size)
        self.postings: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
        for index, document in enumerate(documents):
            for term, frequency in collections.Counter(document).items():
                self.postings[term].append((index, frequency))
        self.df = {term: len(posting) for term, posting in self.postings.items()}

    def idf(self, term: str) -> float:
        frequency = self.df.get(term, 0)
        return math.log(1.0 + (self.size - frequency + 0.5) / (frequency + 0.5))

    def score(self, query: collections.Counter[str]) -> dict[int, float]:
        scores: dict[int, float] = collections.defaultdict(float)
        k1, b = 1.5, 0.75
        for term, query_weight in query.items():
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = self.idf(term)
            for doc_index, frequency in posting:
                denominator = frequency + k1 * (
                    1.0 - b + b * self.lengths[doc_index] / max(1.0, self.average_length)
                )
                scores[doc_index] += idf * (frequency * (k1 + 1.0) / denominator) * query_weight
        return scores


def hierarchy_text(section: dict[str, Any]) -> str:
    return " ".join(value for value in section.get("hierarchy", {}).values() if value)


def informative_query(
    section: dict[str, Any], content_index: BM25Index, max_terms: int = 28
) -> collections.Counter[str]:
    title_counter = collections.Counter(tokens(section["section_title"]))
    hierarchy_counter = collections.Counter(tokens(hierarchy_text(section)))
    content_counter = collections.Counter(tokens(section["text"]))
    weighted: dict[str, float] = {}
    for term, frequency in content_counter.items():
        if term not in content_index.df:
            continue
        weighted[term] = min(3, frequency) * content_index.idf(term)
    selected = sorted(weighted, key=lambda term: (-weighted[term], term))[:max_terms]
    query: collections.Counter[str] = collections.Counter({term: 1.0 for term in selected})
    for term, frequency in hierarchy_counter.items():
        query[term] += min(2, frequency) * 0.7
    for term, frequency in title_counter.items():
        query[term] += min(2, frequency) * 3.0
    return query


def normalize_scores(scores: dict[int, float]) -> dict[int, float]:
    maximum = max(scores.values(), default=0.0)
    return {index: score / maximum for index, score in scores.items()} if maximum else {}


def section_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_candidates(
    fannie: list[dict[str, Any]], freddie: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]]:
    freddie_title_tokens = [tokens(section["section_title"]) for section in freddie]
    freddie_content_tokens = [tokens(section["text"]) for section in freddie]
    title_index = BM25Index(freddie_title_tokens)
    content_index = BM25Index(freddie_content_tokens)
    freddie_title_sets = [set(value) for value in freddie_title_tokens]
    freddie_domains = [set(domains(section["section_title"] + " " + hierarchy_text(section))) for section in freddie]

    rows: list[dict[str, Any]] = []
    for fannie_section in fannie:
        f_title_tokens = tokens(fannie_section["section_title"])
        f_title_counter: collections.Counter[str] = collections.Counter(f_title_tokens)
        title_raw = title_index.score(
            collections.Counter({term: frequency * 2.5 for term, frequency in f_title_counter.items()})
        )
        content_raw = content_index.score(informative_query(fannie_section, content_index))
        title_scores = normalize_scores(title_raw)
        content_scores = normalize_scores(content_raw)
        candidate_indexes = set(sorted(title_raw, key=title_raw.get, reverse=True)[:40])
        candidate_indexes.update(sorted(content_raw, key=content_raw.get, reverse=True)[:40])
        f_title_set = set(f_title_tokens)
        f_domain_set = set(
            domains(fannie_section["section_title"] + " " + hierarchy_text(fannie_section))
        )
        scored: list[tuple[float, int, dict[str, float], list[str], list[str]]] = []
        for index in candidate_indexes:
            m_title_set = freddie_title_sets[index]
            intersection = f_title_set & m_title_set
            union = f_title_set | m_title_set
            coverage = len(intersection) / max(1, len(f_title_set))
            jaccard = len(intersection) / max(1, len(union))
            sequence = difflib.SequenceMatcher(
                None,
                " ".join(sorted(f_title_set)),
                " ".join(sorted(m_title_set)),
            ).ratio()
            common_domains = f_domain_set & freddie_domains[index]
            domain_score = len(common_domains) / max(1, len(f_domain_set | freddie_domains[index]))
            components = {
                "title_relevance": title_scores.get(index, 0.0),
                "text_relevance": content_scores.get(index, 0.0),
                "title_coverage": coverage,
                "title_jaccard": jaccard,
                "title_sequence": sequence,
                "domain_alignment": domain_score,
            }
            score = (
                0.22 * components["title_relevance"]
                + 0.22 * components["text_relevance"]
                + 0.22 * coverage
                + 0.14 * jaccard
                + 0.10 * sequence
                + 0.10 * domain_score
            )
            matched = sorted(
                intersection,
                key=lambda term: (-content_index.idf(term), term),
            )[:12]
            scored.append((score, index, components, matched, sorted(common_domains)))
        scored.sort(key=lambda value: (-value[0], freddie[value[1]]["section_id"]))
        if not scored:
            continue
        top_score = scored[0][0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        for rank, (score, index, components, matched, common_domains) in enumerate(
            scored[:top_k], start=1
        ):
            margin = top_score - second_score if rank == 1 else 0.0
            if rank == 1 and score >= 0.68 and margin >= 0.08 and components["title_coverage"] >= 0.35:
                confidence = "high"
            elif score >= 0.50:
                confidence = "medium"
            else:
                confidence = "low"
            freddie_section = freddie[index]
            rows.append(
                {
                    "crosswalk_id": (
                        f"{fannie_section['section_id']}::{freddie_section['section_id']}"
                    ),
                    "fannie": section_ref(fannie_section),
                    "freddie": section_ref(freddie_section),
                    "rank": rank,
                    "score": round(score, 6),
                    "confidence": confidence,
                    "score_components": {key: round(value, 6) for key, value in components.items()},
                    "matched_terms": matched,
                    "matched_domains": common_domains,
                    "top_rank_margin": round(margin, 6) if rank == 1 else None,
                    "review_status": "machine_suggested",
                    "legal_equivalence": "not_determined",
                }
            )
    return rows


def latest_edition(agency: str) -> Path:
    editions = sorted(p for p in (KB / "editions" / agency).iterdir() if p.is_dir())
    if not editions:
        raise FileNotFoundError(f"No {agency} editions built yet")
    return editions[-1]


def section_ref(section: dict[str, Any]) -> dict[str, Any]:
    keys = ("section_id", "section_record_id", "section_title", "edition_date", "pdf_page_start", "pdf_page_end",
            "citation", "source_url", "official_url")
    return {key: section.get(key) for key in keys}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fannie", type=Path, default=latest_edition("fannie") / "sections.jsonl")
    parser.add_argument("--freddie", type=Path, default=latest_edition("freddie") / "sections.jsonl")
    parser.add_argument("--curated", type=Path, default=KB / "crosswalk" / "curated_pairs.json")
    parser.add_argument("--output", type=Path, default=KB / "crosswalk")
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fannie_path = args.fannie.resolve()
    freddie_path = args.freddie.resolve()
    fannie = [section for section in read_jsonl(fannie_path) if FANNIE_ID_RE.match(section["section_id"])]
    freddie = [
        section
        for section in read_jsonl(freddie_path)
        if section["section_id"] != "GUIDE-NOTICE"
        and section["version_status"] == "current"
        and section["policy_scope"] in {"shared_relationship", "selling"}
    ]
    print(f"Matching {len(fannie)} Fannie topics to {len(freddie)} current Freddie sections ...")
    rows = build_candidates(fannie, freddie, args.top_k)
    curated = json.loads(args.curated.read_text(encoding="utf-8")) if args.curated.exists() else {"pairs": []}
    benchmark: dict[str, set[str]] = collections.defaultdict(set)
    for pair in curated["pairs"]:
        if pair.get("relationship") != "no_counterpart":
            benchmark[pair["fannie_section_id"]].update(pair["freddie_section_ids"])
    known_fannie = {section["section_id"] for section in fannie}
    known_freddie = {section["section_id"] for section in freddie}
    stale_curated = [p for p in curated["pairs"] if p["fannie_section_id"] not in known_fannie
                     or any(fid not in known_freddie for fid in p["freddie_section_ids"])]
    for row in rows:
        if row["freddie"]["section_id"] in benchmark.get(row["fannie"]["section_id"], set()):
            row["review_status"] = "curated_match"
    write_jsonl(output / "crosswalk_candidates.jsonl", rows)

    database = output / "comparison.sqlite"
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE crosswalk_candidates (
                crosswalk_id TEXT PRIMARY KEY,
                fannie_section_id TEXT NOT NULL,
                fannie_section_title TEXT NOT NULL,
                fannie_pdf_page_start INTEGER NOT NULL,
                fannie_pdf_page_end INTEGER NOT NULL,
                fannie_citation TEXT NOT NULL,
                freddie_section_id TEXT NOT NULL,
                freddie_section_record_id TEXT NOT NULL,
                freddie_section_title TEXT NOT NULL,
                freddie_pdf_page_start INTEGER NOT NULL,
                freddie_pdf_page_end INTEGER NOT NULL,
                freddie_citation TEXT NOT NULL,
                candidate_rank INTEGER NOT NULL,
                score REAL NOT NULL,
                confidence TEXT NOT NULL,
                score_components_json TEXT NOT NULL,
                matched_terms_json TEXT NOT NULL,
                matched_domains_json TEXT NOT NULL,
                top_rank_margin REAL,
                review_status TEXT NOT NULL,
                legal_equivalence TEXT NOT NULL
            );
            CREATE INDEX idx_crosswalk_fannie ON crosswalk_candidates(fannie_section_id, candidate_rank);
            CREATE INDEX idx_crosswalk_freddie ON crosswalk_candidates(freddie_section_id, score DESC);
            """
        )
        for row in rows:
            connection.execute(
                "INSERT INTO crosswalk_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["crosswalk_id"], row["fannie"]["section_id"], row["fannie"]["section_title"],
                    row["fannie"]["pdf_page_start"], row["fannie"]["pdf_page_end"], row["fannie"]["citation"],
                    row["freddie"]["section_id"], row["freddie"]["section_record_id"], row["freddie"]["section_title"],
                    row["freddie"]["pdf_page_start"], row["freddie"]["pdf_page_end"], row["freddie"]["citation"],
                    row["rank"], row["score"], row["confidence"],
                    json.dumps(row["score_components"], separators=(",", ":")),
                    json.dumps(row["matched_terms"], separators=(",", ":")),
                    json.dumps(row["matched_domains"], separators=(",", ":")),
                    row["top_rank_margin"], row["review_status"], row["legal_equivalence"],
                ),
            )
        metadata = {
            "fannie_section_count": str(len(fannie)),
            "freddie_current_section_count": str(len(freddie)),
            "candidate_count": str(len(rows)),
            "top_k": str(args.top_k),
        }
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
        connection.commit()
    finally:
        connection.close()

    top_rows = [row for row in rows if row["rank"] == 1]
    review_queue = sorted(
        [row for row in top_rows if row["confidence"] == "low"],
        key=lambda row: (row["score"], row["fannie"]["section_id"]),
    )
    write_jsonl(output / "review_queue.jsonl", review_queue)
    by_fannie: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_fannie[row["fannie"]["section_id"]].append(row)
    benchmark_results = []
    for fannie_id, expected in benchmark.items():
        candidates = sorted(by_fannie[fannie_id], key=lambda row: row["rank"])
        top_three = [row["freddie"]["section_id"] for row in candidates[:3]]
        benchmark_results.append(
            {
                "fannie_section_id": fannie_id,
                "expected_freddie_section_ids": sorted(expected),
                "top_three_freddie_section_ids": top_three,
                "pass": bool(expected & set(top_three)),
            }
        )
    duplicate_pairs = [
        pair for pair, count in collections.Counter(row["crosswalk_id"] for row in rows).items() if count > 1
    ]
    per_fannie_counts = collections.Counter(row["fannie"]["section_id"] for row in rows)
    invalid_candidate_counts = {
        section_id: count for section_id, count in per_fannie_counts.items() if count != args.top_k
    }
    servicing_candidates = [
        row["crosswalk_id"] for row in rows if re.match(r"^[789]\d{3}\.", row["freddie"]["section_id"])
    ]
    benchmark_pass_count = sum(result["pass"] for result in benchmark_results)
    validation_passed = (
        len(top_rows) == len(fannie)
        and not duplicate_pairs
        and not invalid_candidate_counts
        and not servicing_candidates
        and not stale_curated
        and benchmark_pass_count >= math.ceil(len(benchmark_results) * 0.8)
    )
    validation = {
        "status": "pass" if validation_passed else "warn",
        "grain": "one machine-suggested candidate pair per Fannie topic, Freddie section, and rank",
        "checks": {
            "fannie_topic_count": len(fannie),
            "freddie_current_section_count": len(freddie),
            "top_k": args.top_k,
            "candidate_pair_count": len(rows),
            "top_rank_count": len(top_rows),
            "duplicate_pair_count": len(duplicate_pairs),
            "invalid_per_fannie_candidate_count": len(invalid_candidate_counts),
            "servicing_candidate_count": len(servicing_candidates),
            "curated_pair_count": len(curated["pairs"]),
            "stale_curated_pair_count": len(stale_curated),
            "benchmark_case_count": len(benchmark_results),
            "benchmark_top3_pass_count": benchmark_pass_count,
            "benchmark_top3_pass_rate": round(benchmark_pass_count / len(benchmark_results), 4),
            "top_rank_confidence_counts": dict(collections.Counter(row["confidence"] for row in top_rows)),
            "top_rank_score_min": round(min(row["score"] for row in top_rows), 6),
            "top_rank_score_median": round(
                sorted(row["score"] for row in top_rows)[len(top_rows) // 2], 6
            ),
            "top_rank_score_max": round(max(row["score"] for row in top_rows), 6),
        },
        "benchmark_results": benchmark_results,
        "duplicate_pairs": duplicate_pairs,
        "invalid_candidate_counts": invalid_candidate_counts,
        "servicing_candidates": servicing_candidates,
        "stale_curated_pairs": stale_curated,
        "limitations": [
            "Candidate scores are lexical and structural relevance signals, not legal-equivalence judgments.",
            "One Fannie topic may map to multiple Freddie sections and vice versa.",
            "Low-confidence and consequential matches require human mortgage-policy review.",
            "Future-effective Freddie versions are excluded from default candidates.",
        ],
    }
    write_json(output / "validation.json", validation)
    manifest = {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Machine-suggested Fannie-to-Freddie selling-guide candidate crosswalk",
        "inputs": {
            "fannie_sections": args.fannie.resolve().relative_to(KB.parent).as_posix(),
            "fannie_sections_sha256": section_fingerprint(fannie_path),
            "freddie_sections": args.freddie.resolve().relative_to(KB.parent).as_posix(),
            "freddie_sections_sha256": section_fingerprint(freddie_path),
            "freddie_version_policy": "current records only",
        },
        "matching": {
            "method": "deterministic hybrid BM25, title overlap, phrase aliases, and policy-domain alignment",
            "top_k": args.top_k,
            "review_status": "machine_suggested",
            "legal_equivalence": "not_determined",
        },
        "counts": {
            "fannie_topics": len(fannie),
            "freddie_current_sections": len(freddie),
            "candidate_pairs": len(rows),
        },
        "artifacts": {
            "candidate_crosswalk": "crosswalk_candidates.jsonl",
            "comparison_index": "comparison.sqlite",
            "low_confidence_review_queue": "review_queue.jsonl",
            "validation": "validation.json",
        },
    }
    write_json(output / "manifest.json", manifest)
    print(
        f"Built {len(rows)} candidate pairs; benchmark top-3 "
        f"{benchmark_pass_count}/{len(benchmark_results)}; validation={validation['status']}"
    )
    return 0 if validation["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
