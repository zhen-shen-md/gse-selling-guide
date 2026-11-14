#!/usr/bin/env python3
"""Command-line search over the knowledge base (latest editions by default).

Examples:
  python pipeline/query.py search "cash-out refinance"
  python pipeline/query.py search "B3-3.1-04" --agency fannie
  python pipeline/query.py search "rental income" --edition 2026-06-03 --agency fannie
  python pipeline/query.py compare B2-1.3-03
  python pipeline/query.py changes --agency fannie --type revised
  python pipeline/query.py pricing --transaction purchase --score 745 --ltv 78 --attributes condominium

Search uses the per-edition search.sqlite FTS5 index built by the guide builders.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from kb_common import KB, read_jsonl

AGENCIES = {"fannie": "Fannie Mae", "freddie": "Freddie Mac"}


def edition_dir(agency: str, edition: str | None) -> Path:
    editions = sorted(p for p in (KB / "editions" / agency).iterdir() if p.is_dir())
    if edition:
        return KB / "editions" / agency / edition
    return editions[-1]


def fts_query(text: str) -> str:
    if text.startswith('"') and text.endswith('"'):
        return text
    terms = re.findall(r"[A-Za-z0-9.]+(?:-[A-Za-z0-9.]+)*", text)
    return " ".join(f'"{t}"' for t in terms)


def cmd_search(args: argparse.Namespace) -> None:
    for agency in ([args.agency] if args.agency else ["fannie", "freddie"]):
        db = edition_dir(agency, args.edition) / "search.sqlite"
        if not db.exists():
            sys.exit(f"{db} is missing; rebuild the edition (search.sqlite is not committed to git).")
        con = sqlite3.connect(db)
        future = "" if args.include_future else "AND c.version_status = 'current'"
        rows = con.execute(
            f"""SELECT c.section_id, c.section_title, c.version_status, c.effective_date, c.pdf_page_start,
                       c.citation, snippet(chunks_fts, 4, '[', ']', ' … ', 18)
                FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ? {future}
                ORDER BY bm25(chunks_fts, 0, 8, 4, 1, 1) LIMIT ?""",
            (fts_query(args.query), args.limit)).fetchall()
        print(f"\n== {AGENCIES[agency]} ({db.parent.name}) — {len(rows)} results")
        for sid, title, status, eff, page, citation, snip in rows:
            flag = f" [future effective {eff}]" if status == "future" else ""
            print(f"- {sid} {title}{flag}\n  {citation}\n  {snip.replace(chr(10), ' ')}")


def cmd_compare(args: argparse.Namespace) -> None:
    rows = [r for r in read_jsonl(KB / "crosswalk" / "crosswalk_candidates.jsonl")
            if args.section_id in (r["fannie"]["section_id"], r["freddie"]["section_id"])]
    if not rows:
        sys.exit(f"No crosswalk rows for {args.section_id}")
    for r in sorted(rows, key=lambda r: (r["fannie"]["section_id"], r["rank"]))[: args.limit]:
        print(f"#{r['rank']} {r['fannie']['section_id']} {r['fannie']['section_title']}  <->  "
              f"{r['freddie']['section_id']} {r['freddie']['section_title']}  "
              f"[{r['confidence']}, score {r['score']:.2f}, {r['review_status']}]")
    print("\nCandidates are relevance suggestions, not legal-equivalence findings.")


def cmd_changes(args: argparse.Namespace) -> None:
    files = sorted((KB / "changes" / args.agency).glob("*.jsonl"))
    rows = read_jsonl(files[-1])
    if args.type:
        rows = [r for r in rows if r["change_type"] == args.type]
    for r in rows[: args.limit]:
        ann = (r.get("announcement") or {}).get("id", "")
        when = r.get("new_revision_date") or r.get("effective_date") or ""
        print(f"{r['change_type']:12} {r['section_id']:10} {r['section_title'][:70]:70} {when} {ann}")
    print(f"\n{len(rows)} records from {files[-1].name}")


def cmd_pricing(args: argparse.Namespace) -> None:
    records = read_jsonl(KB / "pricing" / "current_records.jsonl")
    attrs = [a for a in (args.attributes or "").split(",") if a]

    def fits(r: dict) -> bool:
        lo, hi = r.get("ltv_min_exclusive"), r.get("ltv_max_inclusive")
        return (lo is None or args.ltv > lo) and (hi is None or args.ltv <= hi)

    for agency in AGENCIES.values():
        base = next((r for r in records if r["agency"] == agency and r.get("pricing_family") == "base"
                     and r["transaction_category"] == args.transaction and fits(r)
                     and (r["score_min_inclusive"] is None or args.score >= r["score_min_inclusive"])
                     and (r["score_max_exclusive"] is None or args.score < r["score_max_exclusive"])), None)
        parts = [("base", base["fee_percent"])] if base else []
        for a in attrs:
            row = next((r for r in records if r["agency"] == agency and r.get("pricing_family") == "attribute"
                        and r["transaction_category"] == args.transaction and r["attribute_key"] == a and fits(r)), None)
            parts.append((a, row["fee_percent"] if row else None))
        total = sum(v for _, v in parts if v is not None)
        print(f"{agency}: {', '.join(f'{k}={v}' for k, v in parts)} -> uncapped total {total:.3f}%")
    print("Uncapped matrix lookup only — not a price quote. Waivers, credits, caps and eligibility are not applied.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--agency", choices=list(AGENCIES))
    s.add_argument("--edition")
    s.add_argument("--include-future", action="store_true")
    s.add_argument("--limit", type=int, default=5)
    c = sub.add_parser("compare")
    c.add_argument("section_id")
    c.add_argument("--limit", type=int, default=10)
    ch = sub.add_parser("changes")
    ch.add_argument("--agency", choices=list(AGENCIES), required=True)
    ch.add_argument("--type")
    ch.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("pricing")
    p.add_argument("--transaction", choices=["purchase", "rate_term_refinance", "cash_out_refinance"], required=True)
    p.add_argument("--score", type=int, required=True)
    p.add_argument("--ltv", type=float, required=True)
    p.add_argument("--attributes")
    args = parser.parse_args()
    {"search": cmd_search, "compare": cmd_compare, "changes": cmd_changes, "pricing": cmd_pricing}[args.command](args)


if __name__ == "__main__":
    main()
