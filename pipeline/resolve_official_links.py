#!/usr/bin/env python3
"""Find and verify links to each Fannie Mae section on the official online Selling Guide.

Fannie's URLs need a title slug (e.g. /sel/b3-5.1-01/general-requirements-credit-scores) whose rules
are not fully predictable, so each candidate slug is fetched and only verified links are kept.
Results are cached in knowledge_base/official_links.json; sections that cannot be resolved fall
back to the version-pinned official PDF page link. Freddie section URLs are deterministic and are
set directly by the Freddie builder.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from kb_common import KB, read_json, read_jsonl, write_json

BASE = "https://selling-guide.fanniemae.com/sel/{sid}/{slug}"
STOPWORDS = set("a an as at by for from in into of off on onto or per than that the this to via with".split())
USER_AGENT = "gse-selling-guide-kb/2.0 (+https://github.com/zhen-shen-md)"


def slugify(title: str, drop_stopwords: bool = True) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower().replace(".", "")
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    if drop_stopwords:
        words = [w for w in words if w not in STOPWORDS]
    return "-".join(words)


def verify(url: str) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            head = response.read(4096).decode("utf-8", "ignore")
            return response.status == 200 and "Page Not Found" not in head
    except (urllib.error.URLError, TimeoutError):
        return False


def resolve(section: dict, older_titles: list[str]) -> str | None:
    sid = section["section_id"].lower()
    tried = []
    for title in [section["section_title"], *older_titles]:
        for drop in (True, False):
            url = BASE.format(sid=sid, slug=slugify(title, drop))
            if url not in tried:
                tried.append(url)
                if verify(url):
                    return url
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Re-check links already in the cache")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    cache_path = KB / "official_links.json"
    cache = read_json(cache_path) if cache_path.exists() else {"fannie": {}}
    editions = sorted(p for p in (KB / "editions" / "fannie").iterdir() if p.is_dir())
    latest = [s for s in read_jsonl(editions[-1] / "sections.jsonl") if re.match(r"^[A-E]\d?-", s["section_id"])]
    older = {}
    for edition in editions[:-1]:
        for s in read_jsonl(edition / "sections.jsonl"):
            older.setdefault(s["section_id"], []).append(s["section_title"])
    todo = [s for s in latest if args.refresh or s["section_id"] not in cache["fannie"]
            or cache["fannie"][s["section_id"]].get("title") != s["section_title"]]
    print(f"Resolving {len(todo)} of {len(latest)} Fannie sections ...", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda s: (s, resolve(s, older.get(s["section_id"], []))), todo))
    for section, url in results:
        cache["fannie"][section["section_id"]] = {"url": url, "title": section["section_title"],
                                                  "checked_on": date.today().isoformat()}
    resolved = sum(1 for v in cache["fannie"].values() if v["url"])
    cache["summary"] = {"fannie_resolved": resolved, "fannie_total": len(cache["fannie"])}
    write_json(cache_path, cache)
    print(f"Verified {resolved}/{len(cache['fannie'])} Fannie section links", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
