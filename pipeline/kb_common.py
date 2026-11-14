"""Helpers shared by the guide, change, crosswalk, and pricing builders."""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "knowledge_base"
SOURCES = ROOT / "sources"
PDF_DIR = SOURCES / "pdfs"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

# Characters that PDF extraction commonly produces for dashes, soft hyphens, etc.
_TRANSLATIONS = {
    "­": "", "￾": "-", "�": "", "‐": "-", "‑": "-", "‒": "-",
    "–": "-", "—": "-", "−": "-", " ": " ",
}


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    for old, new in _TRANSLATIONS.items():
        value = value.replace(old, new)
    return value.replace("\x00", "")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_us_date(value: str) -> tuple[str | None, str | None]:
    """Parse MM/DD/YY or MM/DD/YYYY, tolerating malformed years such as '026'.

    Returns (iso_date, warning). Agency bookmarks occasionally contain typos, so a
    malformed-but-recoverable year is normalized and reported instead of dropped.
    """
    match = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*", value or "")
    if not match:
        return None, f"unparseable date {value!r}"
    month, day, year_raw = int(match.group(1)), int(match.group(2)), match.group(3)
    warning = None
    if len(year_raw) == 4:
        year = int(year_raw)
    elif len(year_raw) == 2:
        year = 2000 + int(year_raw)
    else:
        year = 2000 + int(year_raw) % 100
        warning = f"malformed year in {value!r}; interpreted as {year}"
    try:
        return date(year, month, day).isoformat(), warning
    except ValueError:
        return None, f"invalid calendar date {value!r}"


def parse_long_date(value: str) -> str | None:
    match = re.search(r"([A-Z][a-z]+)\.?\s+(\d{1,2}),\s+(\d{4})", value or "")
    if not match or match.group(1).lower() not in MONTHS:
        return None
    return date(int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))).isoformat()


@dataclass
class OutlineItem:
    title: str
    pdf_page: int
    level: int
    ancestors: list[dict[str, Any]]


def flatten_outline(reader: Any, normalize: Callable[[str], str]) -> list[OutlineItem]:
    flattened: list[OutlineItem] = []

    def walk(items: Iterable[Any], level: int, ancestors: list[dict[str, Any]]) -> None:
        previous: dict[str, Any] | None = None
        for item in items:
            if isinstance(item, list):
                if previous is not None:
                    walk(item, level + 1, ancestors + [previous])
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                previous = None
                continue
            title = normalize(getattr(item, "title", str(item))).replace("\n", " ")
            flattened.append(OutlineItem(title=title, pdf_page=page, level=level, ancestors=list(ancestors)))
            previous = {"title": title, "pdf_page": page, "level": level}

    walk(reader.outline, 0, [])
    return flattened


def extract_pages(pdf_path: Path, normalize: Callable[[str], str], last_page: int | None = None) -> list[str]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    count = len(document) if last_page is None else min(last_page, len(document))
    pages: list[str] = []
    try:
        for index in range(count):
            page = document.get_page(index)
            text_page = page.get_textpage()
            pages.append(normalize(text_page.get_text_range()))
            text_page.close()
            page.close()
            if (index + 1) % 250 == 0:
                print(f"  extracted {index + 1}/{count} pages", flush=True)
    finally:
        document.close()
    return pages


def join_pages(page_texts: list[str], separator: str = "\n\n") -> tuple[str, list[int]]:
    offsets: list[int] = []
    cursor = 0
    for index, text in enumerate(page_texts):
        offsets.append(cursor)
        cursor += len(text) + (len(separator) if index < len(page_texts) - 1 else 0)
    return separator.join(page_texts), offsets


def page_for_offset(offsets: list[int], position: int) -> int:
    return max(1, bisect.bisect_right(offsets, position))


def split_chunks(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + max_chars)
        end = hard_end
        if hard_end < len(text):
            floor = start + int(max_chars * 0.65)
            best = max(text.rfind("\n\n", floor, hard_end), text.rfind("\n", floor, hard_end),
                       text.rfind(". ", floor, hard_end), text.rfind("; ", floor, hard_end))
            if best > floor:
                end = best + (2 if text[best:best + 2] in {". ", "; "} else 1)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap_chars)
        whitespace = re.search(r"\s", text[next_start:min(end, next_start + 120)])
        start = next_start + (whitespace.end() if whitespace else 0)
    return chunks


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def source_manifest() -> dict[str, Any]:
    return read_json(SOURCES / "manifest.json")


def source_entry_for(pdf_path: Path) -> dict[str, Any] | None:
    for doc in source_manifest()["documents"]:
        if doc["local_file"] == pdf_path.name:
            return doc
    return None


def build_fts_index(path: Path, chunks: list[dict[str, Any]], metadata: dict[str, str]) -> None:
    """Build a local SQLite FTS5 index (a convenience artifact; not committed to git)."""
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, section_id TEXT NOT NULL,
                section_record_id TEXT NOT NULL, section_title TEXT NOT NULL, version_status TEXT NOT NULL,
                revision_date TEXT, effective_date TEXT, hierarchy_json TEXT NOT NULL,
                pdf_page_start INTEGER NOT NULL, pdf_page_end INTEGER NOT NULL, citation TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                chunk_id UNINDEXED, section_id, section_title, hierarchy, content,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        for chunk in chunks:
            connection.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chunk["chunk_id"], chunk["document_id"], chunk["section_id"], chunk["section_record_id"],
                 chunk["section_title"], chunk["version_status"], chunk["revision_date"], chunk["effective_date"],
                 json.dumps(chunk["hierarchy"], ensure_ascii=False), chunk["pdf_page_start"],
                 chunk["pdf_page_end"], chunk["citation"], chunk["text"]),
            )
            connection.execute(
                "INSERT INTO chunks_fts VALUES (?,?,?,?,?)",
                (chunk["chunk_id"], chunk["section_id"], chunk["section_title"],
                 " | ".join(v for v in chunk["hierarchy"].values() if v), chunk["text"]),
            )
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
        connection.commit()
    finally:
        connection.close()


def make_chunks(sections: list[dict[str, Any]], max_chars: int, overlap_chars: int) -> list[dict[str, Any]]:
    chunk_fields = ("document_id", "agency", "edition_date", "section_id", "section_record_id", "section_title",
                    "version_status", "revision_date", "effective_date", "policy_scope", "hierarchy",
                    "pdf_page_start", "pdf_page_end", "citation", "source_url", "official_url")
    chunks: list[dict[str, Any]] = []
    for section in sections:
        pieces = split_chunks(section["text"], max_chars, overlap_chars)
        for index, content in enumerate(pieces, start=1):
            chunks.append({
                "chunk_id": f"{section['document_id']}::{section['section_record_id']}::{index:03d}",
                **{key: section.get(key) for key in chunk_fields},
                "chunk_index": index,
                "chunk_count": len(pieces),
                "char_count": len(content),
                "text": content,
            })
    return chunks


def generated_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
