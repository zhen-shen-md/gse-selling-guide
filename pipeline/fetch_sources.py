#!/usr/bin/env python3
"""Download official source PDFs listed in sources/manifest.json and verify their SHA-256.

Exit codes: 0 = every document present and verified; 1 = a download failed;
3 = a hash mismatch, which for a not-version-pinned URL usually means the agency
published a newer edition (the new file is kept as *.new.pdf for review).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "gse-selling-guide-kb/2.0 (+https://github.com/zhen-shen-md)"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        content_type = response.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower():
            raise RuntimeError(f"{url} returned {content_type!r}, not a PDF")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        with tmp.open("wb") as handle:
            while block := response.read(1024 * 1024):
                handle.write(block)
        tmp.replace(target)


def probe_new_fannie_editions(manifest: dict) -> list[str]:
    """Fannie publishes each edition at a dated URL (usually the first Wednesday of a month), so a
    hash check cannot see new editions. Probe the URL pattern for every Wednesday since the latest one."""
    from datetime import date, timedelta

    known = [d["edition_date"] for d in manifest["documents"] if d["agency"] == "Fannie Mae" and d["kind"] == "selling_guide"]
    day = date.fromisoformat(max(known)) + timedelta(days=1)
    found = []
    while day <= date.today():
        if day.weekday() == 2:  # Wednesday
            url = f"https://singlefamily.fanniemae.com/media/document/pdf/selling-guide-{day:%B}-{day.day}-{day.year}".lower()
            request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if "pdf" in response.headers.get("Content-Type", "").lower():
                        found.append(url)
            except Exception:
                pass
        day += timedelta(days=1)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources" / "manifest.json")
    parser.add_argument("--dest", type=Path, default=ROOT / "sources" / "pdfs")
    parser.add_argument("--check-latest", action="store_true",
                        help="Re-download every not-version-pinned URL to detect new editions.")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    status = 0
    entries = manifest["documents"] + [
        {**a, "document_id": a["id"], "sha256": None, "url_is_version_pinned": True}
        for a in manifest.get("announcements", [])
    ]
    for doc in entries:
        target = args.dest / doc["local_file"]
        url = doc.get("url")
        if target.exists() and not (args.check_latest and url and not doc["url_is_version_pinned"]):
            actual = sha256_file(target)
            ok = doc.get("sha256") in (None, actual)
            print(f"{'ok  ' if ok else 'HASH'} {doc['document_id']}")
            if not ok:
                status = max(status, 3)
            continue
        if not url:
            print(f"skip {doc['document_id']} (no public URL for this archived edition)")
            continue
        fresh = target if not target.exists() else target.with_name(target.stem + ".new.pdf")
        try:
            download(url, fresh)
        except Exception as exc:  # network errors are reported, not fatal for the rest
            print(f"FAIL {doc['document_id']}: {exc}")
            status = max(status, 1)
            continue
        actual = sha256_file(fresh)
        if doc.get("sha256") in (None, actual):
            if fresh != target:
                fresh.unlink()
            print(f"ok   {doc['document_id']} (downloaded)")
        else:
            print(f"NEW  {doc['document_id']}: {url} now serves sha256 {actual[:12]}...; saved as {fresh.name}")
            status = max(status, 3)
    if args.check_latest:
        for url in probe_new_fannie_editions(manifest):
            print(f"NEW  Fannie Mae Selling Guide edition published: {url}")
            status = max(status, 3)
    return status


if __name__ == "__main__":
    sys.exit(main())
