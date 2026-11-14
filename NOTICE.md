# Notice on source content

This is an independent research project. It is **not affiliated with, endorsed by, or sponsored by
Fannie Mae, Freddie Mac, or the Federal Housing Finance Agency (FHFA)**.

- Guide and pricing text in `knowledge_base/` is extracted from documents published by Fannie Mae
  (© Fannie Mae) and Freddie Mac (© Freddie Mac). It is reproduced for research and comparison, with
  every record linked back to the official source. The MIT license in `LICENSE` applies to the code only.
- The official PDFs themselves are not redistributed. `pipeline/fetch_sources.py` downloads them from
  the agencies' websites and verifies their SHA-256 fingerprints against `sources/manifest.json`.
- Nothing here is official guidance or a determination of loan eligibility, pricing, or legal
  equivalence between the agencies' requirements. Always rely on the current official guides,
  announcements, bulletins, and your purchase documents.
- If you represent Fannie Mae or Freddie Mac and have concerns about how content is presented,
  please open an issue and it will be addressed promptly.
