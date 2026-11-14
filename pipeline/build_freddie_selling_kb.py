#!/usr/bin/env python3
"""Build one edition of the Freddie Mac Seller/Servicer Guide knowledge base (selling scope).

Usage: python pipeline/build_freddie_selling_kb.py --pdf sources/pdfs/freddie-seller-servicer-guide-2026-11-04.pdf
Output goes to knowledge_base/editions/freddie/<publication date>/ unless --output is given.

Scope: the shared "Freddie Mac - Seller/Servicer Relationship" segment and the "Selling" segment.
The Servicing segment boundary is read from the PDF bookmarks, never hardcoded, because it moves
between editions (PDF page 1,966 in July 2026; page 2,103 in November 2026).
"""

from __future__ import annotations

import argparse
import bisect
import collections
import re
import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from kb_common import (
    KB, build_fts_index, clean_text, extract_pages, flatten_outline, generated_at, join_pages,
    make_chunks, page_for_offset, parse_us_date, sha256_file, sha256_text, source_entry_for,
    write_json, write_jsonl,
)

AGENCY = "Freddie Mac"
GUIDE = "Freddie Mac Single-Family Seller/Servicer Guide (Selling scope)"
SECTION_RE = re.compile(r"^(?P<id>\d{4}\.\d+):\s*(?P<title>.*)$")
VERSION_RE = re.compile(
    r"^(?P<title>.*?)\s+\((?P<future>Future effective date\s+)?(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\)$",
    re.IGNORECASE,
)
GUIDE_HEADER_RE = re.compile(r"^Freddie Mac Single-Family Seller/Servicer (?:Guide|Bulletin)(?:\s+Chapter\s+\d{4})?$")
PAGE_HEADER_RE = re.compile(r"^As of\s+\d{1,2}/\d{1,2}/\d{2,4}\s+Page\s+\d{4}-\d+$")
STRUCTURAL_RE = re.compile(r"^(?:Series|Topic|Chapter)\s+\d{4}:")
NOTICE_RE = re.compile(r"Current as of Bulletin\s+(?P<bulletin>\d{4}-\d+),\s+Published on\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})")
RELATIONSHIP = "Freddie Mac - Seller/Servicer Relationship"
SEGMENTS = {RELATIONSHIP, "Selling", "Servicing"}
OFFICIAL_SECTION_URL = "https://guide.freddiemac.com/app/guide/section/{section_id}"


def normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in clean_text(value).splitlines()]
    cleaned = [line for index, line in enumerate(lines)
               if not (index < 6 and (GUIDE_HEADER_RE.fullmatch(line) or PAGE_HEADER_RE.fullmatch(line)))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def hierarchy_from(item) -> dict[str, str | None]:
    hierarchy: dict[str, str | None] = {"segment": None, "series": None, "topic": None, "chapter": None}
    for ancestor in item.ancestors:
        title = ancestor["title"]
        if title in SEGMENTS:
            hierarchy["segment"] = title
        for key, prefix in (("series", "Series "), ("topic", "Topic "), ("chapter", "Chapter ")):
            if title.startswith(prefix):
                hierarchy[key] = title
    return hierarchy


def find_id_candidates(page_texts: list[str], expected: int, section_id: str) -> list[tuple[int, int]]:
    found = []
    for delta in (0, 1, 2, -1, -2):
        page = expected + delta
        if 1 <= page <= len(page_texts):
            found += [(page, m.start()) for m in re.finditer(rf"(?m)^{re.escape(section_id)}:(?:\s|$)", page_texts[page - 1])]
    return found


def locate_structural(page_texts: list[str], expected: int, heading: str):
    prefix = heading.split(":", 1)[0]
    for delta in (0, 1, 2, -1, -2):
        page = expected + delta
        if 1 <= page <= len(page_texts):
            match = re.search(rf"(?m)^{re.escape(prefix)}:", page_texts[page - 1])
            if match:
                return page, match.start()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--chunk-chars", type=int, default=4500)
    parser.add_argument("--overlap-chars", type=int, default=500)
    parser.add_argument("--no-sqlite", action="store_true")
    args = parser.parse_args()

    source = args.pdf.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    print(f"Reading {source.name} ...", flush=True)
    reader = PdfReader(str(source))
    notice = NOTICE_RE.search(re.sub(r"\s+", " ", reader.pages[0].extract_text() or ""))
    if not notice:
        raise RuntimeError("Page 1 compilation notice not found; is this the compiled Guide PDF?")
    edition, _ = parse_us_date(notice.group("date"))
    bulletin = notice.group("bulletin")
    output = (args.output or KB / "editions" / "freddie" / edition).resolve()
    output.mkdir(parents=True, exist_ok=True)
    document_id = f"freddie-selling-guide-{edition}"
    source_hash = sha256_file(source)
    registry = source_entry_for(source) or {}
    metadata = {str(k).lstrip("/"): str(v) for k, v in (reader.metadata or {}).items()}

    outline = flatten_outline(reader, clean_text)
    segment_pages = {item.title: item.pdf_page for item in outline if item.title in SEGMENTS}
    if "Servicing" not in segment_pages or "Selling" not in segment_pages:
        raise RuntimeError(f"Segment bookmarks not found; saw {sorted(segment_pages)}")
    servicing_first = segment_pages["Servicing"]
    last_included = servicing_first - 1
    print(f"  Bulletin {bulletin} ({edition}); Selling starts p.{segment_pages['Selling']}, "
          f"Servicing starts p.{servicing_first}", flush=True)

    page_texts = extract_pages(source, normalize_text, last_page=last_included)
    document_text, offsets = join_pages(page_texts)
    included = [item for item in outline if item.pdf_page <= last_included]
    canonical = [item for item in included if SECTION_RE.match(item.title)]
    warnings: list[str] = []
    used: set[int] = set()
    items: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for item in canonical:
        match = SECTION_RE.match(item.title)
        section_id, raw_title = match.group("id"), match.group("title")
        version = VERSION_RE.match(raw_title)
        title, status, revised, effective = raw_title, "current", None, None
        if version:
            title = version.group("title")
            parsed, warning = parse_us_date(version.group("date"))
            if warning:
                warnings.append(f"{section_id}: {warning}")
            if version.group("future"):
                status, effective = "future", parsed
            elif parsed and parsed > edition:
                # Bookmark lacks the "Future effective date" label but is dated after the edition.
                status, effective = "future", parsed
                warnings.append(f"{section_id}: dated {parsed} (after edition {edition}) without a "
                                "'Future effective date' label; treated as future-effective")
            else:
                revised = parsed
        candidates = [(p, l, offsets[p - 1] + l) for p, l in find_id_candidates(page_texts, item.pdf_page, section_id)
                      if offsets[p - 1] + l not in used]
        if not candidates:
            missing.append({"section_id": section_id, "title": item.title, "bookmark_pdf_page": item.pdf_page})
            continue
        start = candidates[0][2]
        used.add(start)
        items.append({
            "section_id": section_id,
            "section_record_id": f"{section_id}::future-{effective}" if status == "future" else f"{section_id}::current",
            "section_title": title, "version_status": status, "revision_date": revised, "effective_date": effective,
            "start": start, "hierarchy": hierarchy_from(item),
        })
    items.sort(key=lambda r: (r["start"], r["section_record_id"]))
    if not items:
        raise RuntimeError("No Freddie canonical section headings were located")

    structural = sorted({offsets[loc[0] - 1] + loc[1] for item in included if STRUCTURAL_RE.match(item.title)
                         for loc in [locate_structural(page_texts, item.pdf_page, item.title)] if loc})

    def record(base: dict[str, Any], start: int, end: int) -> dict[str, Any]:
        text = document_text[start:end].strip()
        page_start = page_for_offset(offsets, start)
        sid = base["section_id"]
        return {
            "document_id": document_id, "agency": AGENCY, "guide": GUIDE, "edition_date": edition,
            "bulletin": bulletin, **{k: base[k] for k in ("section_id", "section_record_id", "section_title",
                                                         "version_status", "revision_date", "effective_date", "hierarchy")},
            "policy_scope": "selling" if base["hierarchy"]["segment"] == "Selling" else "shared_relationship",
            "pdf_page_start": page_start, "pdf_page_end": page_for_offset(offsets, max(start, end - 1)),
            "citation": f"Freddie Mac Guide (Bulletin {bulletin}, {edition}), Section {sid}, PDF p. {page_start}",
            "source_url": None,
            "official_url": OFFICIAL_SECTION_URL.format(section_id=sid) if re.match(r"^\d{4}\.\d+$", sid) else None,
            "text": text, "text_sha256": sha256_text(re.sub(r"\s+", " ", text)),
        }

    sections = [record({"section_id": "GUIDE-NOTICE", "section_record_id": "GUIDE-NOTICE::current",
                        "section_title": "Compilation of Guide Chapters", "version_status": "current",
                        "revision_date": None, "effective_date": None,
                        "hierarchy": {"segment": RELATIONSHIP, "series": None, "topic": None, "chapter": None}},
                       0, offsets[1] if len(offsets) > 1 else len(document_text))]
    for index, item in enumerate(items):
        nxt = items[index + 1]["start"] if index + 1 < len(items) else len(document_text)
        boundary = bisect.bisect_right(structural, item["start"])
        nxt_struct = structural[boundary] if boundary < len(structural) else len(document_text)
        sections.append(record(item, item["start"], min(nxt, nxt_struct)))
    sections.sort(key=lambda r: (r["pdf_page_start"], r["section_record_id"]))
    chunks = make_chunks(sections, args.chunk_chars, args.overlap_chars)

    write_jsonl(output / "sections.jsonl", sections)
    write_jsonl(output / "chunks.jsonl", chunks)
    write_json(output / "outline.json", [
        {"title": i.title, "pdf_page": i.pdf_page, "level": i.level,
         "included_in_selling_scope": i.pdf_page <= last_included, "ancestors": [a["title"] for a in i.ancestors]}
        for i in outline])
    if not args.no_sqlite:
        build_fts_index(output / "search.sqlite", chunks, {
            "document_id": document_id, "edition_date": edition, "bulletin": bulletin, "source_sha256": source_hash})

    policy = [s for s in sections if s["section_id"] != "GUIDE-NOTICE"]
    record_ids = [s["section_record_id"] for s in sections]
    heading_mismatches = [s["section_record_id"] for s in policy if not s["text"].startswith(s["section_id"] + ":")]
    heading_count_issues = [s["section_record_id"] for s in policy
                            if re.findall(r"(?m)^(\d{4}\.\d+):", s["text"]) != [s["section_id"]]]
    future_mismatches = [s["section_record_id"] for s in policy if s["version_status"] == "future"
                         and "Future effective date" not in re.sub(r"\s+", " ", s["text"][:1000])
                         and s["effective_date"] not in {w.split("dated ")[-1][:10] for w in warnings}]
    servicing = [s["section_record_id"] for s in policy
                 if s["hierarchy"]["segment"] == "Servicing" or re.match(r"^[789]\d{3}\.", s["section_id"])]
    duplicates = [rid for rid, n in collections.Counter(record_ids).items() if n > 1]
    checks = {
        "source_pdf_page_count": len(reader.pages),
        "selling_segment_first_pdf_page": segment_pages["Selling"],
        "servicing_segment_first_pdf_page": servicing_first,
        "included_pdf_page_count": last_included,
        "outline_item_count": len(outline),
        "selling_scope_outline_section_count": len(canonical),
        "located_policy_section_count": len(policy),
        "unique_policy_section_id_count": len({s["section_id"] for s in policy}),
        "current_policy_record_count": sum(s["version_status"] == "current" for s in policy),
        "future_policy_record_count": sum(s["version_status"] == "future" for s in policy),
        "generated_chunk_count": len(chunks),
        "missing_section_count": len(missing),
        "empty_section_count": sum(1 for s in sections if not s["text"].strip()),
        "heading_mismatch_count": len(heading_mismatches),
        "section_heading_count_issue_count": len(heading_count_issues),
        "future_version_heading_mismatch_count": len(future_mismatches),
        "duplicate_section_record_id_count": len(duplicates),
        "servicing_section_record_count": len(servicing),
        "max_chunk_chars": max(c["char_count"] for c in chunks),
        "date_parse_warning_count": len(warnings),
    }
    passed = not any([missing, heading_mismatches, heading_count_issues, future_mismatches, servicing, duplicates,
                      checks["empty_section_count"]]) and len(policy) == len(canonical)
    validation = {"status": "pass" if passed else "warn", "checks": checks, "missing_sections": missing,
                  "heading_mismatches": heading_mismatches, "section_heading_count_issues": heading_count_issues,
                  "future_version_heading_mismatches": future_mismatches, "duplicate_section_record_ids": duplicates,
                  "warnings": warnings}
    write_json(output / "validation.json", validation)
    write_json(output / "manifest.json", {
        "schema_version": "2.0",
        "generated_at": generated_at(),
        "document": {
            "document_id": document_id, "agency": AGENCY, "guide": GUIDE, "edition_date": edition,
            "bulletin": bulletin, "source_file": source.name, "source_sha256": source_hash,
            "source_url": registry.get("url"), "source_pdf_pages": len(reader.pages), "pdf_metadata": metadata,
        },
        "scope": {
            "included": [f"Compilation notice (PDF p. 1)",
                         f"{RELATIONSHIP} segment (PDF pp. 2-{segment_pages['Selling'] - 1})",
                         f"Selling segment (PDF pp. {segment_pages['Selling']}-{last_included})"],
            "excluded": [f"Servicing segment (PDF p. {servicing_first} onward; Series 7000-9000)",
                         "External materials referenced but not embedded in the PDF"],
            "interpretation": "The shared Seller/Servicer Relationship segment is kept because Freddie Mac states "
                              "that it applies to all Seller/Servicers.",
        },
        "versioning": {"strategy": "current and future-effective versions are separate records "
                                   "(section_record_id '<id>::current' or '<id>::future-<date>')",
                       "current_records": checks["current_policy_record_count"],
                       "future_records": checks["future_policy_record_count"]},
        "chunking": {"strategy": "canonical-section-version-first, then paragraph/line-aware character windows",
                     "max_chars": args.chunk_chars, "overlap_chars": args.overlap_chars},
        "counts": {"outline_items": len(outline), "unique_policy_section_ids": checks["unique_policy_section_id_count"],
                   "policy_section_records": len(policy), "chunks": len(chunks)},
    })
    print(f"Freddie {edition}: {len(sections)} sections, {len(chunks)} chunks; validation={validation['status']}"
          + (f"; {len(warnings)} warnings" if warnings else ""), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
