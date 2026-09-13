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
