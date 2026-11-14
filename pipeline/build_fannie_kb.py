#!/usr/bin/env python3
"""Build one edition of the Fannie Mae Selling Guide knowledge base from its official PDF.

Usage: python pipeline/build_fannie_kb.py --pdf sources/pdfs/fannie-selling-guide-2026-11-04.pdf
Output goes to knowledge_base/editions/fannie/<publication date>/ unless --output is given.
"""

from __future__ import annotations

import argparse
import bisect
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from kb_common import (
    KB, build_fts_index, clean_text, extract_pages, flatten_outline, generated_at, join_pages,
    make_chunks, page_for_offset, parse_long_date, parse_us_date, sha256_file, sha256_text,
    source_entry_for, write_json, write_jsonl,
)

AGENCY = "Fannie Mae"
GUIDE = "Fannie Mae Single Family Selling Guide"
TOPIC_RE = re.compile(r"^(?P<id>[A-E]\d?-\d(?:\.\d+)?-\d{2}),\s*(?P<title>.*)$")
DATED_TITLE_RE = re.compile(r"^(?P<title>.*)\s+\((?P<date>\d{1,2}/\d{1,2}/\d{2,4})\)$")
HEADER_RE = re.compile(r"^Published\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}$")
PAGE_NUMBER_RE = re.compile(r"^(?:\d+|[IVXLCDM]+)$")


def normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in clean_text(value).splitlines()]
    # Each page repeats a "Published <date>" header and a page number; drop them from the top lines only.
    cleaned = [line for index, line in enumerate(lines)
               if not (index < 5 and (HEADER_RE.fullmatch(line) or PAGE_NUMBER_RE.fullmatch(line)))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def find_heading_offset(page_text: str, heading: str, topic_id: str | None = None) -> int | None:
    for prefix in ([topic_id] if topic_id else []) + [heading.split(",", 1)[0]]:
        match = re.search(rf"(?m)^{re.escape(prefix)},(?:\s|$)", page_text)
        if match:
            return match.start()
    return None


def locate_heading(page_texts: list[str], expected: int, heading: str, topic_id: str | None = None):
    """Find a visible heading near its (sometimes imprecise) bookmark page."""
    for delta in (0, 1, 2, -1, -2):
        page = expected + delta
        if 1 <= page <= len(page_texts):
            local = find_heading_offset(page_texts[page - 1], heading, topic_id)
            if local is not None:
                return page, local
    return None


def hierarchy_from(item) -> dict[str, str | None]:
    hierarchy: dict[str, str | None] = {"part": None, "subpart": None, "chapter": None, "section": None}
    for ancestor in item.ancestors:
        for key, prefix in (("part", "Part "), ("subpart", "Subpart "), ("chapter", "Chapter "), ("section", "Section ")):
            if ancestor["title"].startswith(prefix):
                hierarchy[key] = ancestor["title"]
    return hierarchy


def detect_publication_date(reader: PdfReader, override: str | None) -> str:
    if override:
        return override
    candidates = [(reader.pages[0].extract_text() or ""), str((reader.metadata or {}).get("/Title", ""))]
    for text in candidates:
        found = parse_long_date(text)
        if found:
            return found
        dashed = re.search(r"(\d{2})-(\d{2})-(\d{4})", text)
        if dashed:
            return f"{dashed.group(3)}-{dashed.group(1)}-{dashed.group(2)}"
    raise RuntimeError("Could not detect the publication date; pass --edition-date YYYY-MM-DD")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--edition-date", help="Override the detected publication date (YYYY-MM-DD)")
    parser.add_argument("--chunk-chars", type=int, default=4500)
    parser.add_argument("--overlap-chars", type=int, default=500)
    parser.add_argument("--no-sqlite", action="store_true")
    args = parser.parse_args()

    source = args.pdf.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    print(f"Reading {source.name} ...", flush=True)
    reader = PdfReader(str(source))
    edition = detect_publication_date(reader, args.edition_date)
    output = (args.output or KB / "editions" / "fannie" / edition).resolve()
    output.mkdir(parents=True, exist_ok=True)
    document_id = f"fannie-selling-guide-{edition}"
    source_hash = sha256_file(source)
    registry = source_entry_for(source) or {}
    pinned_url = registry.get("url") if registry.get("url_is_version_pinned") else None
    metadata = {str(k).lstrip("/"): str(v) for k, v in (reader.metadata or {}).items()}

    page_texts = extract_pages(source, normalize_text)
    document_text, offsets = join_pages(page_texts)
    outline = flatten_outline(reader, clean_text)
    warnings: list[str] = []

    topic_items: list[dict[str, Any]] = []
    missing_topics: list[dict[str, Any]] = []
    for item in outline:
        match = TOPIC_RE.match(item.title)
        if not match:
            continue
        topic_id, raw_title = match.group("id"), match.group("title")
        dated = DATED_TITLE_RE.match(raw_title)
        title = dated.group("title") if dated else raw_title
        revised = None
        if dated:
            revised, warning = parse_us_date(dated.group("date"))
            if warning:
                warnings.append(f"{topic_id}: {warning}")
        location = locate_heading(page_texts, item.pdf_page, item.title, topic_id)
        if location is None:
            missing_topics.append({"section_id": topic_id, "title": item.title, "pdf_page": item.pdf_page})
            continue
        page, local = location
        topic_items.append({"section_id": topic_id, "section_title": title, "revision_date": revised,
                            "start": offsets[page - 1] + local, "hierarchy": hierarchy_from(item)})
    topic_items.sort(key=lambda row: (row["start"], row["section_id"]))
    if not topic_items:
        raise RuntimeError("No canonical topic headings were located")

    structural_offsets = sorted({
        offsets[loc[0] - 1] + loc[1]
        for item in outline if re.match(r"^(?:Part|Subpart|Chapter|Section)\s", item.title)
        for loc in [locate_heading(page_texts, item.pdf_page, item.title)] if loc
    })

    def section(section_id: str, title: str, start: int, end: int, hierarchy: dict, revised: str | None) -> dict:
        text = document_text[start:end].strip()
        page_start = page_for_offset(offsets, start)
        return {
            "document_id": document_id, "agency": AGENCY, "guide": GUIDE, "edition_date": edition,
            "section_id": section_id, "section_record_id": section_id, "section_title": title,
            "version_status": "current", "revision_date": revised, "effective_date": None,
            "policy_scope": "selling", "hierarchy": hierarchy,
            "pdf_page_start": page_start, "pdf_page_end": page_for_offset(offsets, max(start, end - 1)),
            "citation": f"{GUIDE} ({edition}), {section_id}, PDF p. {page_start}",
            "source_url": f"{pinned_url}#page={page_start}" if pinned_url else None,
            "official_url": None,
            "text": text, "text_sha256": sha256_text(re.sub(r"\s+", " ", text)),
        }

    sections: list[dict[str, Any]] = []
    preface = next(item for item in outline if item.title == "Preface")
    first_part = next(item for item in outline if item.title.startswith("Part A,"))
    preface_start = offsets[preface.pdf_page - 1] + (find_heading_offset(page_texts[preface.pdf_page - 1], preface.title) or 0)
    part_start = offsets[first_part.pdf_page - 1] + (find_heading_offset(page_texts[first_part.pdf_page - 1], first_part.title) or 0)
    sections.append(section("PREFACE", "Preface", preface_start, part_start,
                            {"part": "Preface", "subpart": None, "chapter": None, "section": None}, None))

    for part in (item for item in outline if item.level == 0 and re.match(r"^Part [A-E],", item.title)):
        local = find_heading_offset(page_texts[part.pdf_page - 1], part.title)
        if local is None:
            continue
        start = offsets[part.pdf_page - 1] + local
        first_topic = next((t for t in topic_items if t["hierarchy"]["part"] == part.title), None)
        if first_topic and first_topic["start"] > start:
            code = part.title[5]
            sections.append(section(f"PART-{code}-INTRO", part.title, start, first_topic["start"],
                                    {"part": part.title, "subpart": None, "chapter": None, "section": None}, None))

    for index, topic in enumerate(topic_items):
        start = topic["start"]
        next_topic = topic_items[index + 1]["start"] if index + 1 < len(topic_items) else len(document_text)
        boundary = bisect.bisect_right(structural_offsets, start)
        next_structural = structural_offsets[boundary] if boundary < len(structural_offsets) else len(document_text)
        sections.append(section(topic["section_id"], topic["section_title"], start, min(next_topic, next_structural),
                                topic["hierarchy"], topic["revision_date"]))

    sections.sort(key=lambda r: (r["pdf_page_start"], 0 if r["section_id"] == "PREFACE" else 1, r["section_id"]))
    chunks = make_chunks(sections, args.chunk_chars, args.overlap_chars)

    write_jsonl(output / "sections.jsonl", sections)
    write_jsonl(output / "chunks.jsonl", chunks)
    write_json(output / "outline.json", [
        {"title": i.title, "pdf_page": i.pdf_page, "level": i.level, "ancestors": [a["title"] for a in i.ancestors]}
        for i in outline])
    if not args.no_sqlite:
        build_fts_index(output / "search.sqlite", chunks, {
            "document_id": document_id, "edition_date": edition, "source_sha256": source_hash})

    topic_sections = [s for s in sections if TOPIC_RE.match(s["section_id"] + ",")]
    edition_day = date.fromisoformat(edition)
    header_text = f"Published {edition_day:%B} {edition_day.day}, {edition_day.year}"
    mismatches = [s["section_id"] for s in topic_sections if not s["text"].startswith(s["section_id"] + ",")]
    checks = {
        "pdf_page_count": len(reader.pages),
        "outline_item_count": len(outline),
        "outline_topic_count": len(topic_items) + len(missing_topics),
        "located_topic_count": len(topic_items),
        "missing_topic_count": len(missing_topics),
        "generated_section_count": len(sections),
        "generated_topic_section_count": len(topic_sections),
        "generated_chunk_count": len(chunks),
        "empty_section_count": sum(1 for s in sections if not s["text"].strip()),
        "empty_chunk_count": sum(1 for c in chunks if not c["text"].strip()),
        "topic_heading_mismatch_count": len(mismatches),
        "duplicate_section_id_count": len(sections) - len({s["section_id"] for s in sections}),
        "max_chunk_chars": max(c["char_count"] for c in chunks),
        "forbidden_noncharacter_count": sum(s["text"].count("￾") for s in sections),
        "repeated_publication_headers_remaining": sum(s["text"].count(header_text) for s in sections),
        "date_parse_warning_count": len(warnings),
    }
    passed = (not missing_topics and not mismatches and checks["empty_section_count"] == 0
              and checks["empty_chunk_count"] == 0 and checks["forbidden_noncharacter_count"] == 0
              and checks["duplicate_section_id_count"] == 0)
    validation = {"status": "pass" if passed else "warn", "checks": checks, "missing_topics": missing_topics,
                  "topic_heading_mismatches": mismatches, "warnings": warnings}
    write_json(output / "validation.json", validation)
    write_json(output / "manifest.json", {
        "schema_version": "2.0",
        "generated_at": generated_at(),
        "document": {
            "document_id": document_id, "agency": AGENCY, "guide": GUIDE, "edition_date": edition,
            "source_file": source.name, "source_sha256": source_hash, "source_url": registry.get("url"),
            "pdf_pages": len(reader.pages), "pdf_metadata": metadata,
        },
        "scope": {
            "included": "Preface and Parts A-E of the Fannie Mae Single Family Selling Guide",
            "excluded": ["Cover, copyright notice, and table of contents",
                         "External materials incorporated by reference but not embedded in the PDF"],
        },
        "chunking": {"strategy": "canonical-topic-first, then paragraph/line-aware character windows",
                     "max_chars": args.chunk_chars, "overlap_chars": args.overlap_chars},
        "counts": {"outline_items": len(outline), "topic_sections": len(topic_sections),
                   "all_sections": len(sections), "chunks": len(chunks)},
    })
    print(f"Fannie {edition}: {len(sections)} sections, {len(chunks)} chunks; validation={validation['status']}"
          + (f"; {len(warnings)} warnings" if warnings else ""), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
