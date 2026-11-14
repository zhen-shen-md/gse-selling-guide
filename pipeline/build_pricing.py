#!/usr/bin/env python3
"""Extract Fannie Mae LLPA and Freddie Mac Exhibit 19 credit-fee grids from each agency's own PDF,
then compare them cell by cell and track changes between pricing document versions.

Usage: python pipeline/build_pricing.py
  Extracts every pricing document listed in sources/manifest.json (kind = pricing) that is present
  locally, compares the latest Fannie and Freddie documents, and diffs each agency's latest document
  against its previous one.

Grid cells are parsed from PDF text (never hand-typed), so a difference between the agencies or an
extraction failure is reported rather than masked. Fixed-dollar credits and fee waivers/caps are short
lists transcribed in knowledge_base/pricing/curated_provisions.json; this script re-verifies that the
quoted text still appears in each PDF.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from kb_common import KB, PDF_DIR, clean_text, generated_at, read_json, sha256_file, source_manifest, write_json, write_jsonl

OUT = KB / "pricing"
PCT = r"\d+\.\d+\s?%"
ROW_RE = re.compile(rf"^(?P<label>.*?)\s*(?P<vals>(?:{PCT}\s*){{4,}})(?P<tail>.*)$")
LTV9 = [("lte_30", None, 30.0), ("gt_30_lte_60", 30.0, 60.0), ("gt_60_lte_70", 60.0, 70.0),
        ("gt_70_lte_75", 70.0, 75.0), ("gt_75_lte_80", 75.0, 80.0), ("gt_80_lte_85", 80.0, 85.0),
        ("gt_85_lte_90", 85.0, 90.0), ("gt_90_lte_95", 90.0, 95.0), ("gt_95", 95.0, None)]
LTV5 = LTV9[:5]
LTV_MI = [("gt_80_lte_85", 80.0, 85.0), ("gt_85_lte_90", 85.0, 90.0), ("gt_90_lte_95", 90.0, 95.0),
          ("gt_95_lte_97", 95.0, 97.0)]
ATTRIBUTES = [  # (key, pattern) — first match wins
    ("alt_fico", r"alt\s*fico"),
    ("adjustable_rate", r"adjustable"),
    ("condominium", r"condo"),
    ("investment_property", r"investment"),
    ("second_home", r"second home"),
    ("manufactured_home", r"manufactured"),
    ("two_to_four_units", r"two- to four|number of units"),
    ("high_balance_arm", r"high[- ]balance arm|super conforming arm"),
    ("high_balance_fixed_rate", r"high[- ]balance fixed|super conforming frm"),
    ("subordinate_financing", r"subordinate|secondary financing"),
]
TRANSACTION_LABELS = {
    "purchase": "Purchase",
    "rate_term_refinance": "Fannie limited cash-out / Freddie no cash-out refinance",
    "cash_out_refinance": "Cash-out refinance",
}


def page_texts(path: Path) -> list[str]:
    document = pdfium.PdfDocument(str(path))
    try:
        return [clean_text(document.get_page(i).get_textpage().get_text_range()) for i in range(len(document))]
    finally:
        document.close()


def page_context(agency: str, text: str) -> tuple[str | None, str | None]:
    """Return (family_hint, transaction) for a grid page, or (None, None) if it holds no grid."""
    head = " ".join(text.splitlines()[:14])
    if agency == "Fannie Mae":
        if re.search(r"Minimum Mortgage Insurance Coverage Option", head):
            return "mortgage_insurance", None
        if re.search(r"Limited Cash-out Refinance", head, re.I):
            return "grid", "rate_term_refinance"
        if re.search(r"Cash-out Refinance Loans", head, re.I):
            return "grid", "cash_out_refinance"
        if re.search(r"Purchase Money Loans", head, re.I):
            return "grid", "purchase"
    else:
        if re.search(r"CUSTOM MORTGAGE INSURANCE OPTIONS", text):
            return "mortgage_insurance", None
        if re.search(r"Section 4: No Cash-out Refinance", head):
            return "grid", "rate_term_refinance"
        if re.search(r"Section 5: Cash-out Refinance", head):
            return "grid", "cash_out_refinance"
        if re.search(r"Section 3: Purchase", head):
            return "grid", "purchase"
    return None, None


def parse_score(label: str) -> tuple[int | None, int | None, str | None] | None:
    """Parse a credit-score band label into (min_inclusive, max_exclusive, note)."""
    s = label.replace("–", "-").replace("≥ =", "≥").replace(" ", "")
    if m := re.fullmatch(r"≥(\d{3})&<(\d{3})", s):
        return int(m[1]), int(m[2]), None
    if m := re.fullmatch(r"(\d{3})-(\d{3})", s):
        return int(m[1]), int(m[2]) + 1, None
    if m := re.fullmatch(r"≥(\d{3})", s):
        return int(m[1]), None, None
    if m := re.fullmatch(r">(\d{3})", s):
        return int(m[1]), None, (f"Source label reads '> {m[1]}'; the next band ends at {int(m[1]) - 1}, so it is "
                                 f"read as '≥ {m[1]}' to avoid leaving a score of {m[1]} uncovered.")
    if m := re.fullmatch(r"≤(\d{3})", s):
        return None, int(m[1]) + 1, None
    if m := re.fullmatch(r"<(\d{3})", s):
        return None, int(m[1]), None
    return None


def score_key(lo: int | None, hi: int | None) -> str:
    if lo is not None and hi is None:
        return f"gte_{lo}"
    if lo is None and hi is not None:
        return f"lt_{hi}"
    return f"{lo}_{hi - 1}"


def attribute_key(label: str) -> tuple[str, str]:
    clean = re.sub(r"[\d,\s]+$", "", label).strip()  # drop trailing footnote markers like "Condo1" or "8, 9"
    for key, pattern in ATTRIBUTES:
        if re.search(pattern, clean, re.I):
            return key, clean
    return re.sub(r"[^a-z0-9]+", "_", clean.lower()).strip("_"), clean


def extract_document(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str]:
    path = PDF_DIR / doc["local_file"]
    texts = page_texts(path)
    agency, notes, records = doc["agency"], [], []
    for page_no, text in enumerate(texts, start=1):
        family_hint, transaction = page_context(agency, text)
        if not family_hint:
            continue
        for line in text.splitlines():
            m = ROW_RE.match(line.strip())
            if not m:
                continue
            values = [float(v.replace("%", "").strip()) for v in re.findall(PCT, m["vals"])]
            label = m["label"].strip()
            tail = m["tail"].strip()
            score = parse_score(label) if label else None
            if family_hint == "mortgage_insurance":
                if not score or len(values) != 4:
                    notes.append(f"p{page_no}: skipped MI row {line.strip()[:80]!r}")
                    continue
                family, bands, attr, attr_label = "mortgage_insurance", LTV_MI, None, None
            elif score:
                family, attr, attr_label = "base", None, None
                bands = LTV9 if len(values) == 9 else LTV5 if len(values) == 5 else None
            else:
                family = "attribute"
                attr, attr_label = attribute_key(label)
                bands = LTV9 if len(values) == 9 else LTV5 if len(values) == 5 else None
            if bands is None:
                notes.append(f"p{page_no}: unexpected column count {len(values)} in {line.strip()[:80]!r}")
                continue
            if score and score[2]:
                notes.append(f"p{page_no}: {score[2]}")
            sfc = tail if agency == "Fannie Mae" and tail and tail.upper() not in ("N/A", "NA") else None
            for (band, lo, hi), value in zip(bands, values):
                rec = {
                    "record_type": "price_adjustment_grid_cell", "agency": agency,
                    "document_id": doc["document_id"], "effective_date": doc["edition_date"],
                    "pricing_family": family, "transaction_category": transaction,
                    "attribute_key": attr, "attribute_label": attr_label,
                    "score_band": score_key(score[0], score[1]) if score else "all_scores",
                    "score_min_inclusive": score[0] if score else None,
                    "score_max_exclusive": score[1] if score else None,
                    "ltv_band": band, "ltv_min_exclusive": lo, "ltv_max_inclusive": hi,
                    "fee_percent": value, "fee_unit": "percent_of_unpaid_principal_balance",
                    "special_feature_code": sfc, "source_page": page_no,
                    "source_url": (doc.get("url") + f"#page={page_no}") if doc.get("url") else None,
                    "source_row_text": line.strip(),
                }
                key = [agency, family, transaction or "all", attr or "none", rec["score_band"], band]
                rec["record_id"] = f"{doc['document_id']}::" + "::".join(str(k).lower().replace(" ", "_") for k in key)
                records.append(rec)
    return records, notes, "\n".join(texts)


def cell_key(row: dict[str, Any]) -> tuple:
    return (row["pricing_family"], row["transaction_category"], row["attribute_key"], row["score_band"], row["ltv_band"])


def compare(left: list[dict], right: list[dict], left_name: str, right_name: str) -> tuple[list[dict], list[dict]]:
    a = {cell_key(r): r for r in left}
    b = {cell_key(r): r for r in right}
    pairs, unmatched = [], []
    for key in sorted(a.keys() | b.keys(), key=str):
        x, y = a.get(key), b.get(key)
        if x and y:
            delta = round(x["fee_percent"] - y["fee_percent"], 3)
            pairs.append({
                "comparison_id": "::".join(str(k) for k in key), "pricing_family": key[0],
                "transaction_category": key[1], "attribute_key": key[2], "score_band": key[3], "ltv_band": key[4],
                f"{left_name}_value": x["fee_percent"], f"{right_name}_value": y["fee_percent"],
                "delta": delta, "relationship": "equal" if delta == 0 else (f"{left_name}_higher" if delta > 0 else f"{right_name}_higher"),
                f"{left_name}_record_id": x["record_id"], f"{right_name}_record_id": y["record_id"],
                f"{left_name}_source_page": x["source_page"], f"{right_name}_source_page": y["source_page"],
            })
        else:
            row = x or y
            unmatched.append({"only_in": left_name if x else right_name, "pricing_family": key[0],
                              "transaction_category": key[1], "attribute_key": key[2],
                              "attribute_label": row.get("attribute_label"), "score_band": key[3], "ltv_band": key[4],
                              "fee_percent": row["fee_percent"], "record_id": row["record_id"]})
    return pairs, unmatched


def main() -> int:
    docs = [d for d in source_manifest()["documents"] if d["kind"] == "pricing" and (PDF_DIR / d["local_file"]).exists()]
    curated = read_json(OUT / "curated_provisions.json")
    by_agency: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        by_agency[doc["agency"]].append(doc)
    for agency_docs in by_agency.values():
        agency_docs.sort(key=lambda d: d["edition_date"])

    extracted: dict[str, list[dict]] = {}
    doc_checks: dict[str, dict] = {}
    for doc in docs:
        records, notes, full_text = extract_document(doc)
        extracted[doc["document_id"]] = records
        provisions = [p for p in curated["provisions"] if doc["document_id"] in p["document_ids"]]
        flat = re.sub(r"\s+", " ", full_text)
        missing_quotes = [p["record_id"] for p in provisions for q in p["verify_text"] if re.sub(r"\s+", " ", q) not in flat]
        fam = Counter(r["pricing_family"] for r in records)
        doc_checks[doc["document_id"]] = {
            "sha256_matches_manifest": sha256_file(PDF_DIR / doc["local_file"]) == doc["sha256"],
            "grid_cells": len(records), "cells_by_family": dict(fam),
            "base_cells": fam["base"], "mortgage_insurance_cells": fam["mortgage_insurance"],
            "attributes": sorted({r["attribute_key"] for r in records if r["attribute_key"]}),
            "duplicate_record_ids": len(records) - len({r["record_id"] for r in records}),
            "curated_provisions_checked": len(provisions), "curated_quotes_missing": missing_quotes,
            "notes": notes,
        }
        write_jsonl(OUT / "documents" / f"{doc['document_id']}.jsonl",
                    records + [{**{k: v for k, v in p.items() if k not in ("document_ids", "verify_text")},
                                "agency": doc["agency"], "document_id": doc["document_id"],
                                "effective_date": doc["edition_date"], "record_id": f"{doc['document_id']}::{p['record_id']}",
                                "source_url": (doc.get("url") + f"#page={p['source_page']}") if doc.get("url") else None}
                               for p in provisions])
        print(f"{doc['document_id']}: {len(records)} grid cells {dict(fam)}; {len(provisions)} curated provisions"
              + (f"; MISSING QUOTES {missing_quotes}" if missing_quotes else ""), flush=True)

    latest = {agency: agency_docs[-1] for agency, agency_docs in by_agency.items()}
    fannie_doc, freddie_doc = latest["Fannie Mae"], latest["Freddie Mac"]
    pairs, unmatched = compare(extracted[fannie_doc["document_id"]], extracted[freddie_doc["document_id"]], "fannie", "freddie")
    write_jsonl(OUT / "comparisons.jsonl", pairs)
    write_jsonl(OUT / "unmatched_cells.jsonl", unmatched)
    current_records = extracted[fannie_doc["document_id"]] + extracted[freddie_doc["document_id"]]
    write_jsonl(OUT / "current_records.jsonl", current_records + [
        {**{k: v for k, v in p.items() if k not in ("document_ids", "verify_text")},
         "agency": d["agency"], "document_id": d["document_id"], "effective_date": d["edition_date"],
         "record_id": f"{d['document_id']}::{p['record_id']}",
         "source_url": (d.get("url") + f"#page={p['source_page']}") if d.get("url") else None}
        for d in (fannie_doc, freddie_doc) for p in curated["provisions"] if d["document_id"] in p["document_ids"]])

    # Version-to-version changes per agency.
    version_changes = []
    for agency, agency_docs in by_agency.items():
        if len(agency_docs) < 2:
            continue
        old, new = agency_docs[-2], agency_docs[-1]
        changed, only = compare(extracted[old["document_id"]], extracted[new["document_id"]], "old", "new")
        version_changes.append({
            "agency": agency, "old_document_id": old["document_id"], "new_document_id": new["document_id"],
            "old_effective_date": old["edition_date"], "new_effective_date": new["edition_date"],
            "compared_cells": len(changed), "changed_cells": [c for c in changed if c["relationship"] != "equal"],
            "cells_only_in_old": [u for u in only if u["only_in"] == "old"],
            "cells_only_in_new": [u for u in only if u["only_in"] == "new"],
        })
    write_json(OUT / "version_changes.json", {"generated_at": generated_at(), "changes": version_changes})

    rel = Counter(p["relationship"] for p in pairs)
    by_family = defaultdict(Counter)
    for p in pairs:
        by_family[p["pricing_family"]][p["relationship"]] += 1
    only_attr = sorted({(u["only_in"], u["attribute_label"] or u["attribute_key"] or u["pricing_family"], u["transaction_category"])
                        for u in unmatched}, key=str)
    findings = [{"finding": f"{fam.replace('_', ' ').title()} grids",
                 "evidence": f"{c['equal']} of {sum(c.values())} aligned cells are equal"
                             + (f"; {sum(c.values()) - c['equal']} differ" if sum(c.values()) != c['equal'] else "") + "."}
                for fam, c in sorted(by_family.items())]
    for vc in version_changes:
        findings.append({"finding": f"{vc['agency']}: {vc['old_effective_date']} → {vc['new_effective_date']}",
                         "evidence": f"{len(vc['changed_cells'])} cell values changed; {len(vc['cells_only_in_old'])} cells removed"
                                     + (f" ({', '.join(sorted({u['attribute_label'] or u['attribute_key'] or u['pricing_family'] for u in vc['cells_only_in_old']}))} rows)" if vc['cells_only_in_old'] else "")
                                     + f"; {len(vc['cells_only_in_new'])} cells added"
                                     + (f" ({', '.join(sorted({u['attribute_label'] or u['attribute_key'] or u['pricing_family'] for u in vc['cells_only_in_new']}))} rows)" if vc['cells_only_in_new'] else "")
                                     + "."})
    for only_in, label, txn in only_attr:
        findings.append({"finding": f"Only in {'Fannie Mae' if only_in == 'fannie' else 'Freddie Mac'}: {label}",
                         "evidence": f"Transaction: {TRANSACTION_LABELS.get(txn, 'all')}. No like-for-like row in the other agency's grid."})
    findings.append({"finding": "Program rules still differ",
                     "evidence": "Equal fee numbers do not make eligibility, waivers, caps, delivery codes, or effective-date rules interchangeable."})
    headline = (f"All {len(pairs)} like-for-like percentage cells are equal" if rel["equal"] == len(pairs)
                else f"{rel['equal']} of {len(pairs)} like-for-like percentage cells are equal; {len(pairs) - rel['equal']} differ")
    write_json(OUT / "insights.json", {
        "as_of": max(fannie_doc["edition_date"], freddie_doc["edition_date"]),
        "fannie_document_id": fannie_doc["document_id"], "freddie_document_id": freddie_doc["document_id"],
        "headline": headline + f" (Fannie LLPA Matrix {fannie_doc['edition_date']} vs Freddie Exhibit 19 {freddie_doc['edition_date']}).",
        "findings": findings,
    })

    latest_checks = [doc_checks[d["document_id"]] for d in (fannie_doc, freddie_doc)]
    checks = {
        "all_source_hashes_match": all(c["sha256_matches_manifest"] for c in doc_checks.values()),
        "no_duplicate_cells": all(c["duplicate_record_ids"] == 0 for c in doc_checks.values()),
        "latest_base_cells_207_each": all(c["base_cells"] == 207 for c in latest_checks),
        "latest_mi_cells_32_each": all(c["mortgage_insurance_cells"] == 32 for c in latest_checks),
        "all_curated_quotes_found": all(not c["curated_quotes_missing"] for c in doc_checks.values()),
        "comparison_count": len(pairs),
    }
    status = "pass" if all(v for k, v in checks.items() if k != "comparison_count") else "fail"
    write_json(OUT / "validation.json", {"status": status, "generated_at": generated_at(), "checks": checks,
                                         "documents": doc_checks})
    write_json(OUT / "manifest.json", {
        "schema_version": "2.0", "generated_at": generated_at(),
        "scope": "Fannie Mae LLPAs and Freddie Mac Credit Fees (selling-side pricing only)",
        "current_pair": {"fannie": fannie_doc["document_id"], "freddie": freddie_doc["document_id"]},
        "documents": [{k: d.get(k) for k in ("document_id", "agency", "edition_date", "bulletin", "url", "sha256")} for d in docs],
        "method": "Grid cells parsed from each agency's PDF text; credits and waivers transcribed in curated_provisions.json "
                  "and re-verified against PDF text on every build.",
        "normalization": {
            "transaction_mapping": {"Fannie limited cash-out refinance": "rate_term_refinance",
                                    "Freddie no cash-out refinance": "rate_term_refinance"},
            "delta_definition": "Fannie value minus Freddie value",
        },
        "counts": {"current_records": len(current_records), "comparisons": len(pairs), "unmatched_cells": len(unmatched)},
    })
    print(f"Comparison: {headline}; unmatched {len(unmatched)}; validation={status}", flush=True)
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
