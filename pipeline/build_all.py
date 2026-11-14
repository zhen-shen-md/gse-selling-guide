#!/usr/bin/env python3
"""Rebuild everything that is out of date, in order.

  1. Build any guide edition listed in sources/manifest.json whose PDF is present but not yet built.
  2. Rebuild change logs (previous edition -> latest edition, per agency).
  3. Rebuild the crosswalk, pricing extraction, and (optionally) official-link verification.
  4. Validate, then package site data.

Usage: python pipeline/build_all.py [--rebuild-editions] [--links]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from kb_common import KB, PDF_DIR, ROOT, source_manifest

PY = sys.executable
PIPE = ROOT / "pipeline"
BUILDERS = {"Fannie Mae": ("fannie", "build_fannie_kb.py"), "Freddie Mac": ("freddie", "build_freddie_selling_kb.py")}


def run(script: str, *args: str) -> None:
    print(f"\n$ python pipeline/{script} {' '.join(args)}", flush=True)
    result = subprocess.run([PY, str(PIPE / script), *args], cwd=ROOT)
    if result.returncode not in (0,):
        raise SystemExit(f"{script} failed with exit code {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild-editions", action="store_true", help="Re-extract editions even if already built")
    parser.add_argument("--links", action="store_true", help="Re-verify Fannie official links (network)")
    parser.add_argument("--rebuild-changes", action="store_true", help="Recompute existing change logs")
    args = parser.parse_args()

    for doc in source_manifest()["documents"]:
        if doc["kind"] != "selling_guide":
            continue
        agency_key, script = BUILDERS[doc["agency"]]
        built = (KB / "editions" / agency_key / doc["edition_date"] / "sections.jsonl").exists()
        pdf = PDF_DIR / doc["local_file"]
        if pdf.exists() and (args.rebuild_editions or not built):
            run(script, "--pdf", str(pdf.relative_to(ROOT)))
        elif not built:
            print(f"skip {doc['document_id']}: PDF not downloaded (run pipeline/fetch_sources.py)")

    for agency_key in ("fannie", "freddie"):
        eds = sorted(p.name for p in (KB / "editions" / agency_key).iterdir() if p.is_dir())
        # One change log per consecutive pair of editions (month by month when monthly editions exist).
        wanted = {f"{a}__{b}" for a, b in zip(eds, eds[1:])}
        for old, new in zip(eds, eds[1:]):
            if args.rebuild_changes or not (KB / "changes" / agency_key / f"{old}__{new}.jsonl").exists():
                run("build_changes.py", "--agency", agency_key, "--old", old, "--new", new)
        for stale in (KB / "changes" / agency_key).glob("*__*.jsonl"):
            if stale.stem not in wanted:
                print(f"removing non-consecutive change log {stale.name}")
                stale.unlink()
                stale.with_suffix(".summary.json").unlink(missing_ok=True)
        # Retention: only the latest edition keeps chunks.jsonl (the RAG layer); older editions keep
        # sections.jsonl, which is all that change detection needs.
        for ed in eds[:-1]:
            (KB / "editions" / agency_key / ed / "chunks.jsonl").unlink(missing_ok=True)
    run("build_guide_crosswalk.py")
    if any((PDF_DIR / d["local_file"]).exists() for d in source_manifest()["documents"] if d["kind"] == "pricing"):
        run("build_pricing.py")
    if args.links:
        run("resolve_official_links.py")
    run("validate_knowledge_bases.py")
    run("build_site_data.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
