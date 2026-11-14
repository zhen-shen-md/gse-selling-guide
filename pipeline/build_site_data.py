#!/usr/bin/env python3
"""Package the committed knowledge base into compact JSON files for the web app.

Reads only knowledge_base/ (no PDFs needed), so CI can run it to publish the static site.
Output: web/public/data/*.json (generated; not committed).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from kb_common import KB, ROOT, generated_at, read_json, read_jsonl, source_manifest

OUT = ROOT / "web" / "public" / "data"
MAX_DIFF_SENTENCES = 15


def latest(agency: str) -> Path:
    return sorted(p for p in (KB / "editions" / agency).iterdir() if p.is_dir())[-1]


def write(name: str, data) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (OUT / name).write_text(payload, encoding="utf-8", newline="\n")
    return len(payload.encode("utf-8"))


def hierarchy_text(h: dict) -> str:
    return " › ".join(v for v in (h or {}).values() if v)


def main() -> int:
    links = read_json(KB / "official_links.json").get("fannie", {}) if (KB / "official_links.json").exists() else {}
    sizes, meta_editions = {}, {}
    for agency in ("fannie", "freddie"):
        directory = latest(agency)
        manifest = read_json(directory / "manifest.json")
        sections = read_jsonl(directory / "sections.jsonl")
        compact = []
        for s in sections:
            official = s.get("official_url") or (links.get(s["section_id"]) or {}).get("url")
            compact.append({
                "id": s["section_id"], "rid": s["section_record_id"], "t": s["section_title"],
                "st": s["version_status"], "rev": s.get("revision_date"), "eff": s.get("effective_date"),
                "h": hierarchy_text(s.get("hierarchy")), "p": s["pdf_page_start"], "src": s.get("source_url"),
                "off": official, "x": s["text"],
            })
        sizes[f"sections-{agency}.json"] = write(f"sections-{agency}.json", compact)
        doc = manifest["document"]
        meta_editions[agency] = {
            "agency": doc["agency"], "edition_date": doc["edition_date"], "bulletin": doc.get("bulletin"),
            "document_id": doc["document_id"], "source_url": doc.get("source_url"), "guide": doc["guide"],
            "sections": sum(1 for s in sections if s["version_status"] == "current"),
            "future_sections": sum(1 for s in sections if s["version_status"] == "future"),
            "all_editions": sorted(p.name for p in (KB / "editions" / agency).iterdir() if p.is_dir()),
        }

    changes = {}
    for agency in ("fannie", "freddie"):
        periods, records = [], []
        for path in sorted((KB / "changes" / agency).glob("*__*.jsonl")):
            summary = read_json(path.with_suffix(".summary.json"))
            period = f"{summary['old_edition']}__{summary['new_edition']}"
            periods.append({"id": period, "old": summary["old_edition"], "new": summary["new_edition"],
                            "counts": summary["counts"], "byMonth": summary["by_month"]})
            for r in read_jsonl(path):
                diff = r.get("diff") or {}
                records.append({
                    "cid": r["change_id"], "period": period, "type": r["change_type"], "sid": r["section_id"],
                    "osid": r["old_section_id"], "nsid": r["new_section_id"], "title": r["section_title"],
                    "otitle": r["old_title"], "orev": r["old_revision_date"], "nrev": r["new_revision_date"],
                    "eff": r["effective_date"], "h": hierarchy_text(r.get("hierarchy")), "src": r.get("source_url"),
                    "off": r.get("official_url") or (links.get(r["new_section_id"] or "") or {}).get("url"),
                    "ann": r.get("announcement"), "cited": r.get("cited_in_announcements", []),
                    "sim": diff.get("similarity"), "sa": diff.get("sentences_added"), "sr": diff.get("sentences_removed"),
                    "add": (diff.get("added") or [])[:MAX_DIFF_SENTENCES], "rem": (diff.get("removed") or [])[:MAX_DIFF_SENTENCES],
                    "more": bool(diff.get("truncated")) or len(diff.get("added") or []) > MAX_DIFF_SENTENCES
                            or len(diff.get("removed") or []) > MAX_DIFF_SENTENCES,
                    "succ": r.get("successors", []),
                })
        if periods:
            changes[agency] = {"old": periods[0]["old"], "new": periods[-1]["new"], "periods": periods, "records": records}
    sizes["changes.json"] = write("changes.json", changes)

    crosswalk_rows = read_jsonl(KB / "crosswalk" / "crosswalk_candidates.jsonl")
    by_fannie: dict[str, list] = {}
    for r in crosswalk_rows:
        by_fannie.setdefault(r["fannie"]["section_id"], []).append(
            [r["freddie"]["section_id"], r["rank"], r["score"], r["confidence"], r["review_status"]])
    curated = read_json(KB / "crosswalk" / "curated_pairs.json")["pairs"]
    sizes["crosswalk.json"] = write("crosswalk.json", {"byFannie": by_fannie, "curated": curated})

    pricing_dir = KB / "pricing"
    sizes["pricing.json"] = write("pricing.json", {
        "records": read_jsonl(pricing_dir / "current_records.jsonl"),
        "insights": read_json(pricing_dir / "insights.json"),
        "versionChanges": read_json(pricing_dir / "version_changes.json")["changes"],
        "unmatched": read_jsonl(pricing_dir / "unmatched_cells.jsonl"),
        "manifest": read_json(pricing_dir / "manifest.json"),
    })

    validation = read_json(KB / "validation_summary.json") if (KB / "validation_summary.json").exists() else {}
    sources = source_manifest()
    sizes["meta.json"] = write("meta.json", {
        "generated_at": generated_at(),
        "editions": meta_editions,
        "announcements": sources.get("announcements", []),
        "sources": [{k: d.get(k) for k in ("document_id", "agency", "kind", "edition_date", "bulletin", "url")}
                    for d in sources["documents"]],
        "validation": {"status": validation.get("status", "unknown"), "failures": validation.get("failures", [])},
        "pricingHeadline": read_json(pricing_dir / "insights.json")["headline"],
    })
    total = sum(sizes.values())
    for name, size in sizes.items():
        print(f"  {name:24} {size / 1e6:6.2f} MB")
    print(f"Site data written to {OUT.relative_to(ROOT)} ({total / 1e6:.1f} MB before compression)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
