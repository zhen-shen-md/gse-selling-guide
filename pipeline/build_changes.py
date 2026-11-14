#!/usr/bin/env python3
"""Compare two editions of one agency's guide and emit section-level change records.

Usage:
  python pipeline/build_changes.py --agency fannie --old 2026-06-03 --new 2026-11-04
  python pipeline/build_changes.py --agency freddie --old 2026-07-01 --new 2026-11-04

Sections are matched across editions in this order, so renumbering is not mistaken for
an add + remove: (1) same ID and same title, (2) same title under a different ID,
(3) same ID with a similar title, (4) high text similarity. Output:
knowledge_base/changes/<agency>/<old>__<new>.jsonl and a summary JSON beside it.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from kb_common import KB, PDF_DIR, generated_at, read_jsonl, source_manifest, write_json, write_jsonl

MAX_SENTENCES = 40
MAX_SENTENCE_CHARS = 600
NOISE_RES = [
    re.compile(r"(?m)^As of\s+\d{1,2}/\d{1,2}/\d{2,4}\s+Page\s+\d{4}-\d+\s*$"),
    re.compile(r"(?m)^Freddie Mac Single-Family Seller/Servicer (?:Guide|Bulletin)(?:\s+Chapter\s+\d{4})?\s*$"),
    re.compile(r"(?m)^Published\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\s*$"),
    re.compile(r"(?m)^\d{1,4}\s*$"),
]
FANNIE_ID_RE = re.compile(r"\b[A-E]\d?-\d(?:\.\d+)?-\d{2}\b")
SUBSTANTIVE = {"added", "removed", "split", "merged", "restructured", "revised", "renumbered", "retitled", "took_effect",
               "upcoming", "upcoming_revised"}


def norm_title(value: str) -> str:
    value = value.lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def body_for_diff(section: dict[str, Any]) -> str:
    text = section["text"]
    # Drop the heading line: it carries the revision date, which is reported separately.
    text = text.split("\n", 1)[1] if "\n" in text else ""
    for pattern in NOISE_RES:
        text = pattern.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;:!?])\s+(?=[A-Z(•▪\-0-9])|\s+(?=•|▪)", text)
    return [p.strip() for p in parts if len(p.strip()) > 2]


def shingles(text: str, k: int = 5) -> set[int]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {hash(" ".join(words[i:i + k])) for i in range(max(0, len(words) - k + 1))}


def jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def sentence_diff(old_text: str, new_text: str) -> dict[str, Any]:
    old_s, new_s = sentences(old_text), sentences(new_text)
    matcher = difflib.SequenceMatcher(a=old_s, b=new_s, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += old_s[i1:i2]
        if tag in ("replace", "insert"):
            added += new_s[j1:j2]
    clip = lambda rows: [r[:MAX_SENTENCE_CHARS] + ("…" if len(r) > MAX_SENTENCE_CHARS else "") for r in rows[:MAX_SENTENCES]]
    return {
        "similarity": round(difflib.SequenceMatcher(a=old_text, b=new_text, autojunk=False).quick_ratio(), 4),
        "sentences_added": len(added), "sentences_removed": len(removed),
        "added": clip(added), "removed": clip(removed),
        "truncated": len(added) > MAX_SENTENCES or len(removed) > MAX_SENTENCES,
    }


def word_bag_similarity(a: str, b: str) -> float:
    """Multiset overlap of words; ~1.0 means the same words in a different order (table reflow)."""
    ca, cb = Counter(re.findall(r"[a-z0-9$%.]+", a.lower())), Counter(re.findall(r"[a-z0-9$%.]+", b.lower()))
    total = sum((ca | cb).values())
    return sum((ca & cb).values()) / total if total else 1.0


def classify_text_change(o: dict, n: dict, diff: dict) -> str:
    """Separate substantive revisions from layout reflow and editorial touch-ups."""
    old_body, new_body = body_for_diff(o), body_for_diff(n)
    if word_bag_similarity(old_body, new_body) >= 0.985:
        return "layout_only"
    if o.get("revision_date") == n.get("revision_date"):
        # Text changed but the agency did not re-date the section: cross-reference
        # renumbering, navigation links, or formatting. Kept, but flagged as editorial.
        return "editorial"
    return "revised"


def same_body(a: dict, b: dict) -> bool:
    """True when two records carry the same policy text apart from the heading/date line."""
    x, y = body_for_diff(a), body_for_diff(b)
    return x == y or word_bag_similarity(x, y) >= 0.985


def successors(removed: dict, candidates: list[dict], threshold: float = 0.08) -> list[dict[str, Any]]:
    """Which new-edition sections absorbed a removed section's text (split or merge)."""
    old_sh = shingles(body_for_diff(removed))
    found = []
    for n in candidates:
        if not old_sh:
            break
        contained = len(old_sh & shingles(body_for_diff(n))) / len(old_sh)
        if contained >= threshold:
            found.append({"section_id": n["section_id"], "section_title": n["section_title"],
                          "share_of_old_text": round(contained, 3)})
    return sorted(found, key=lambda r: -r["share_of_old_text"])


def match_sections(old: list[dict], new: list[dict]) -> list[tuple[dict | None, dict | None, str]]:
    """Return (old, new, how) pairs; unmatched rows appear with None on the other side."""
    pairs: list[tuple[dict | None, dict | None, str]] = []
    old_left = {s["section_id"]: s for s in old}
    new_left = {s["section_id"]: s for s in new}

    def take(o: dict, n: dict, how: str) -> None:
        pairs.append((o, n, how))
        old_left.pop(o["section_id"], None)
        new_left.pop(n["section_id"], None)

    for sid in list(new_left):
        if sid in old_left and norm_title(old_left[sid]["section_title"]) == norm_title(new_left[sid]["section_title"]):
            take(old_left[sid], new_left[sid], "same_id_same_title")
    by_title = {}
    for o in old_left.values():
        by_title.setdefault(norm_title(o["section_title"]), []).append(o)
    for sid, n in list(new_left.items()):
        candidates = by_title.get(norm_title(n["section_title"]), [])
        candidates = [c for c in candidates if c["section_id"] in old_left]
        if len(candidates) == 1:
            take(candidates[0], n, "same_title_new_id")
    for sid in list(new_left):
        if sid in old_left:
            ratio = difflib.SequenceMatcher(a=norm_title(old_left[sid]["section_title"]),
                                            b=norm_title(new_left[sid]["section_title"])).ratio()
            body_sim = jaccard(shingles(body_for_diff(old_left[sid])), shingles(body_for_diff(new_left[sid])))
            if ratio >= 0.6 or body_sim >= 0.3:
                take(old_left[sid], new_left[sid], "same_id_similar")
    if old_left and new_left:
        old_sh = {sid: shingles(body_for_diff(s)) for sid, s in old_left.items()}
        new_sh = {sid: shingles(body_for_diff(s)) for sid, s in new_left.items()}
        scored = sorted(((jaccard(old_sh[o], new_sh[n]), o, n) for o in old_sh for n in new_sh), reverse=True)
        for score, o, n in scored:
            if score < 0.35:
                break
            if o in old_left and n in new_left:
                take(old_left[o], new_left[n], f"text_similarity_{score:.2f}")
    pairs += [(o, None, "unmatched") for o in old_left.values()]
    pairs += [(None, n, "unmatched") for n in new_left.values()]
    return pairs


def announcement_index(agency_name: str) -> tuple[dict[str, dict], dict[str, set[str]]]:
    """Map announcement dates to announcements and (for Fannie) announcement IDs to cited section IDs."""
    by_date, cited = {}, {}
    for ann in source_manifest().get("announcements", []):
        if ann["agency"] != agency_name:
            continue
        by_date[ann["date"]] = ann
        path = PDF_DIR / ann["local_file"]
        if path.exists():
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
            cited[ann["id"]] = set(FANNIE_ID_RE.findall(text))
    return by_date, cited


def change_record(agency: str, old_ed: str, new_ed: str, o: dict | None, n: dict | None, how: str,
                  change_type: str, diff: dict | None, ann: dict | None, cited_by: list[str]) -> dict[str, Any]:
    cur = n or o
    return {
        "change_id": f"{agency}::{old_ed}__{new_ed}::{(o or {}).get('section_record_id', '-')}->{(n or {}).get('section_record_id', '-')}",
        "agency": cur["agency"], "old_edition": old_ed, "new_edition": new_ed, "change_type": change_type,
        "match_method": how,
        "section_id": cur["section_id"], "section_title": cur["section_title"],
        "old_section_id": o and o["section_id"], "new_section_id": n and n["section_id"],
        "old_title": o and o["section_title"], "new_title": n and n["section_title"],
        "old_revision_date": o and o.get("revision_date"), "new_revision_date": n and n.get("revision_date"),
        "effective_date": (n or {}).get("effective_date"),
        "version_status": (n or o).get("version_status"),
        "hierarchy": cur.get("hierarchy"),
        "old_pdf_page": o and o["pdf_page_start"], "new_pdf_page": n and n["pdf_page_start"],
        "official_url": cur.get("official_url"), "source_url": (n or {}).get("source_url") or (o or {}).get("source_url"),
        "announcement": ann and {"id": ann["id"], "date": ann["date"], "url": ann["url"]},
        "cited_in_announcements": cited_by,
        "diff": diff,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agency", choices=["fannie", "freddie"], required=True)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    args = parser.parse_args()

    base = KB / "editions" / args.agency
    old_rows = [s for s in read_jsonl(base / args.old / "sections.jsonl") if re.match(r"^[A-E\d]", s["section_id"])]
    new_rows = [s for s in read_jsonl(base / args.new / "sections.jsonl") if re.match(r"^[A-E\d]", s["section_id"])]
    agency_name = new_rows[0]["agency"]
    by_date, cited = announcement_index(agency_name)

    def links(section: dict | None) -> tuple[dict | None, list[str]]:
        if not section:
            return None, []
        ann = by_date.get(section.get("revision_date") or "") or by_date.get(section.get("effective_date") or "")
        citing = sorted(aid for aid, ids in cited.items() if section["section_id"] in ids)
        return ann, citing

    records: list[dict[str, Any]] = []
    old_cur = [s for s in old_rows if s["version_status"] == "current"]
    new_cur = [s for s in new_rows if s["version_status"] == "current"]
    old_future = {s["section_record_id"]: s for s in old_rows if s["version_status"] == "future"}

    for o, n, how in match_sections(old_cur, new_cur):
        if o and n:
            same_text = o["text_sha256"] == n["text_sha256"]
            diff = None if same_text else sentence_diff(body_for_diff(o), body_for_diff(n))
            if diff and diff["sentences_added"] == 0 and diff["sentences_removed"] == 0:
                diff, same_text = None, True  # only heading/date or layout noise changed
            renumbered = o["section_id"] != n["section_id"]
            retitled = norm_title(o["section_title"]) != norm_title(n["section_title"])
            if same_text and not renumbered and not retitled:
                if o.get("revision_date") == n.get("revision_date"):
                    continue  # unchanged: not emitted
                change_type = "date_only"
            elif renumbered:
                change_type = "renumbered"
            elif same_text:
                change_type = "retitled"
            else:
                change_type = classify_text_change(o, n, diff)
            # Freddie: a future version present in the old edition that is now the current text.
            if args.agency == "freddie" and not same_text and any(
                    fut["section_id"] == n["section_id"] and same_body(fut, n) for fut in old_future.values()):
                change_type = "took_effect"
                diff = diff or sentence_diff(body_for_diff(o), body_for_diff(n))
            ann, citing = links(n)
            records.append(change_record(args.agency, args.old, args.new, o, n, how, change_type, diff, ann, citing))
        elif n:
            ann, citing = links(n)
            was_future = args.agency == "freddie" and any(
                fut["section_id"] == n["section_id"] and same_body(fut, n) for fut in old_future.values())
            records.append(change_record(args.agency, args.old, args.new, None, n, how,
                                         "took_effect" if was_future else "added", None, ann, citing))
        else:
            rec = change_record(args.agency, args.old, args.new, o, None, how, "removed", None, None, [])
            rec["successors"] = successors(o, new_cur)
            if rec["successors"]:
                rec["change_type"] = "split" if len(rec["successors"]) > 1 else "merged"
                ann, citing = links(next(s for s in new_cur if s["section_id"] == rec["successors"][0]["section_id"]))
                rec["announcement"] = ann and {"id": ann["id"], "date": ann["date"], "url": ann["url"]}
            records.append(rec)

    # An old section that disappeared and a new section that reuses its ID: report one "restructured" change.
    added_by_id = {r["new_section_id"]: r for r in records if r["change_type"] in ("added", "took_effect")}
    for r in [r for r in records if r["change_type"] in ("removed", "merged", "split")]:
        a = added_by_id.get(r["old_section_id"])
        if not a:
            continue
        o = next(s for s in old_cur if s["section_id"] == r["old_section_id"])
        n = next(s for s in new_cur if s["section_id"] == a["new_section_id"])
        merged = change_record(args.agency, args.old, args.new, o, n, "same_id_restructured", "restructured",
                               sentence_diff(body_for_diff(o), body_for_diff(n)),
                               *links(n))
        merged["successors"] = r.get("successors", [])
        records.remove(r)
        records.remove(a)
        records.append(merged)

    if args.agency == "freddie":
        current_by_id = {s["section_id"]: s for s in new_cur}
        for n in (s for s in new_rows if s["version_status"] == "future"):
            previous = old_future.get(n["section_record_id"])
            if previous and same_body(previous, n):
                continue  # already announced in the old edition, unchanged
            base_row = current_by_id.get(n["section_id"])
            diff = sentence_diff(body_for_diff(base_row), body_for_diff(n)) if base_row else None
            ann, citing = links(n)
            records.append(change_record(args.agency, args.old, args.new, base_row, n,
                                         "future_vs_current", "upcoming" if not previous else "upcoming_revised",
                                         diff, ann, citing))

    records.sort(key=lambda r: (r["change_type"], r["section_id"]))
    out = KB / "changes" / args.agency / f"{args.old}__{args.new}.jsonl"
    write_jsonl(out, records)
    counts = Counter(r["change_type"] for r in records)
    date_mismatch = [r["section_id"] for r in records if r["change_type"] == "editorial"]
    summary = {
        "generated_at": generated_at(), "agency": agency_name, "old_edition": args.old, "new_edition": args.new,
        "counts": dict(sorted(counts.items())),
        "substantive_change_count": sum(1 for r in records if r["change_type"] in SUBSTANTIVE),
        "by_month": dict(sorted(Counter((r["new_revision_date"] or r["effective_date"] or "")[:7] for r in records
                                        if r["change_type"] in SUBSTANTIVE and (r["new_revision_date"] or r["effective_date"])).items())),
        "announcements": sorted({r["announcement"]["id"] for r in records if r["announcement"]}),
        "quality": {
            "text_changed_without_revision_date_change": date_mismatch,
            "note": "'editorial' = text changed but the agency did not re-date the section (usually cross-reference "
                    "renumbering or navigation links); 'layout_only' = same words, different PDF text order.",
        },
    }
    write_json(out.with_suffix(".summary.json"), summary)
    print(f"{agency_name} {args.old} -> {args.new}: {dict(counts)}; "
          f"{len(date_mismatch)} text changes without a date change", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
