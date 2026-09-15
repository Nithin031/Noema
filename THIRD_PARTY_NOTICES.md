# Third-party notices

Noema is licensed under the Mozilla Public License 2.0 (see `LICENSE.txt`).
This file records third-party components and their licenses. A per-dependency
license table lives in
[`src/noema/docs/licensing/dependency-audit.md`](src/noema/docs/licensing/dependency-audit.md).

## ActivityWatch (interoperability reference)

- Project: ActivityWatch (`https://github.com/ActivityWatch/activitywatch`)
- License: **Mozilla Public License 2.0**.
- Role in Noema: protocol/behavior reference for the native telemetry
  collectors and the optional, read-only compatibility adapter. Details in
  [`src/noema/docs/licensing/activitywatch.md`](src/noema/docs/licensing/activitywatch.md).
- Code copied or modified into Noema: **none**. The native collectors in
  `src/noema/infrastructure/activity_sources/native_*.py` are independent
  implementations using only the Python standard library.

## Meme Center dataset corpus (local asset source, not distributed)

- Dataset: third-party meme sentiment-analysis corpus (~6,992 images plus
  `labels.csv` with OCR text and sentiment labels).
- License: distribution listed as **GPL-2.0**; **individual image rights are
  unknown** (internet memes of third-party origin).
- Role in Noema: optional local asset source for the Meme Center. Ingestion
  indexes metadata (filenames, OCR, sentiment labels) into the local
  database and caches thumbnails locally; sentiment labels are preserved
  verbatim as dataset metadata and are never treated as intervention
  suitability, tone, or quality judgments.
- Redistribution: **none**. The corpus lives outside the repository
  (`/images/` is gitignored), is never committed, and Noema ships no
  dataset images. Provenance (`source`, `source_ref`, license note) is
  stored per asset in `meme_assets`.

## Runtime dependency

- `google-genai` (Apache-2.0) — hosted (Gemini) classification SDK, imported
  only when the hosted provider is active. See the dependency audit for the
  transitive set (all permissive).

## Notes on retained artifacts

- Root `LICENSE.txt` is the standard MPL-2.0 text — the correct license for
  Noema itself.
- `CITATION.cff` describes **Noema** (it previously carried ActivityWatch's
  upstream citation metadata; that has been corrected).
- This repository does **not** bundle a web frontend or browser extension,
  so there is no `web/LICENSE` and no third-party dashboard/extension assets
  to notice here. If a frontend or assets are added, record their licenses in
  this file and in the dependency audit before distributing them.
