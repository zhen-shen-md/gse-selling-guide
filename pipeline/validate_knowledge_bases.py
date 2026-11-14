#!/usr/bin/env python3
"""Cross-check every committed knowledge-base artifact and write knowledge_base/validation_summary.json
and knowledge_base/catalog.json.

Reads JSON/JSONL only (no PDFs, no SQLite), so it runs in CI on a fresh clone.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from kb_common import KB, generated_at, read_json, read_jsonl, write_json


def editions(agency: str) -> list[Path]:
    return sorted(p for p in (KB / "editions" / agency).iterdir() if p.is_dir())


def main() -> int:
    checks: dict[str, bool] = {}
    counts: dict[str, int] = {}
    failures: list[str] = []
    catalog_editions = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = bool(ok)
        if not ok:
            failures.append(f"{name}{': ' + detail if detail else ''}")

    sections_by_edition: dict[tuple[str, str], list[dict]] = {}
    for agency in ("fannie", "freddie"):
        eds = editions(agency)
        check(f"{agency}_has_two_or_more_editions", len(eds) >= 2, f"found {len(eds)}")
        for ed in eds:
            tag = f"{agency}_{ed.name}"
            validation = read_json(ed / "validation.json")
            manifest = read_json(ed / "manifest.json")
            sections = read_jsonl(ed / "sections.jsonl")
            is_latest = ed == eds[-1]
            chunks = read_jsonl(ed / "chunks.jsonl") if (ed / "chunks.jsonl").exists() else []
            sections_by_edition[(agency, ed.name)] = sections
            check(f"{tag}_builder_validation_pass", validation["status"] == "pass")
            check(f"{tag}_record_ids_unique", len({s["section_record_id"] for s in sections}) == len(sections))
            if is_latest:
                check(f"{tag}_chunk_ids_unique", len({c["chunk_id"] for c in chunks}) == len(chunks))
                check(f"{tag}_every_section_has_chunks",
                      {s["section_record_id"] for s in sections} == {c["section_record_id"] for c in chunks})
            check(f"{tag}_no_empty_text", all(s["text"].strip() for s in sections))
            check(f"{tag}_links_are_https", all((s.get(k) or "https://").startswith("https://")
                                               for s in sections for k in ("source_url", "official_url")))
            if agency == "freddie":
                check(f"{tag}_no_servicing_sections", not any(re.match(r"^[789]\d{3}\.", s["section_id"]) for s in sections))
            counts[f"{tag}_sections"] = len(sections)
            counts[f"{tag}_chunks"] = len(chunks)
            catalog_editions.append({
                "agency": manifest["document"]["agency"], "edition_date": ed.name,
                "document_id": manifest["document"]["document_id"], "bulletin": manifest["document"].get("bulletin"),
                "directory": f"editions/{agency}/{ed.name}", "source_url": manifest["document"].get("source_url"),
                "source_sha256": manifest["document"]["source_sha256"],
                "current_sections": sum(s["version_status"] == "current" for s in sections),
                "future_sections": sum(s["version_status"] == "future" for s in sections),
            })

    change_files: dict[str, list[str]] = {}
    for agency in ("fannie", "freddie"):
        eds = editions(agency)
        change_files[agency] = []
        kinds: Counter = Counter()
        for old_ed, new_ed in zip(eds, eds[1:]):
            expected = KB / "changes" / agency / f"{old_ed.name}__{new_ed.name}.jsonl"
            tag = f"{agency}_{old_ed.name}__{new_ed.name}"
            check(f"{tag}_change_log_exists", expected.exists())
            if not expected.exists():
                continue
            rows = read_jsonl(expected)
            change_files[agency].append(f"changes/{agency}/{expected.name}")
            old_ids = {s["section_id"] for s in sections_by_edition[(agency, old_ed.name)]}
            new_ids = {s["section_id"] for s in sections_by_edition[(agency, new_ed.name)]}
            check(f"{tag}_old_ids_resolve", all(r["old_section_id"] in old_ids for r in rows if r["old_section_id"]))
            check(f"{tag}_new_ids_resolve", all(r["new_section_id"] in new_ids for r in rows if r["new_section_id"]))
            check(f"{tag}_change_ids_unique", len({r["change_id"] for r in rows}) == len(rows))
            kinds.update(r["change_type"] for r in rows)
        extra = {p.stem for p in (KB / "changes" / agency).glob("*__*.jsonl")} - {
            f"{a.name}__{b.name}" for a, b in zip(eds, eds[1:])}
        check(f"{agency}_no_non_consecutive_change_logs", not extra, ", ".join(sorted(extra)))
        counts[f"{agency}_change_records"] = sum(kinds.values())
        for kind, n in kinds.items():
            counts[f"{agency}_changes_{kind}"] = n

    cw_validation = read_json(KB / "crosswalk" / "validation.json")
    cw = read_jsonl(KB / "crosswalk" / "crosswalk_candidates.jsonl")
    latest_f = {s["section_id"] for s in sections_by_edition[("fannie", editions("fannie")[-1].name)]}
    latest_r = {s["section_id"] for s in sections_by_edition[("freddie", editions("freddie")[-1].name)]}
    check("crosswalk_validation_pass", cw_validation["status"] == "pass")
    check("crosswalk_ids_resolve_to_latest_editions",
          all(r["fannie"]["section_id"] in latest_f and r["freddie"]["section_id"] in latest_r for r in cw))
    counts["crosswalk_candidates"] = len(cw)
    counts["crosswalk_low_confidence_top_matches"] = cw_validation["checks"]["top_rank_confidence_counts"].get("low", 0)
    counts["crosswalk_curated_pairs"] = cw_validation["checks"]["curated_pair_count"]

    pricing_validation = read_json(KB / "pricing" / "validation.json")
    check("pricing_validation_pass", pricing_validation["status"] == "pass")
    counts["pricing_comparisons"] = pricing_validation["checks"]["comparison_count"]

    links = read_json(KB / "official_links.json") if (KB / "official_links.json").exists() else {"fannie": {}}
    check("official_links_https", all((v["url"] or "https://").startswith("https://") for v in links["fannie"].values()))
    counts["fannie_official_links_verified"] = sum(1 for v in links["fannie"].values() if v["url"])

    status = "pass" if not failures else "fail"
    write_json(KB / "validation_summary.json", {"status": status, "generated_at": generated_at(),
                                                "checks": checks, "counts": counts, "failures": failures})
    write_json(KB / "catalog.json", {
        "schema_version": "2.0",
        "description": "Registry of knowledge-base artifacts. Paths are relative to knowledge_base/.",
        "editions": catalog_editions,
        "change_logs": change_files,
        "crosswalk": {"candidates": "crosswalk/crosswalk_candidates.jsonl", "curated_pairs": "crosswalk/curated_pairs.json",
                      "review_queue": "crosswalk/review_queue.jsonl"},
        "pricing": {"current_records": "pricing/current_records.jsonl", "comparisons": "pricing/comparisons.jsonl",
                    "version_changes": "pricing/version_changes.json", "insights": "pricing/insights.json"},
        "official_links": "official_links.json",
    })
    print(f"validation={status}; {sum(checks.values())}/{len(checks)} checks passed")
    for failure in failures:
        print(f"  FAIL {failure}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
