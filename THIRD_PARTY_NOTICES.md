# Third-party notices

## ActivityWatch (upstream reference)

- Project: ActivityWatch (`https://github.com/ActivityWatch/activitywatch`)
- License: **Mozilla Public License 2.0** (see upstream `LICENSE.txt`).
- Role in Noema: behavior/protocol reference for the native telemetry
  collectors and the optional legacy compatibility adapter. Details in
  `docs/licensing/activitywatch.md`.
- Code copied or modified into Noema: **none**. The native collectors in
  `src/noema/infrastructure/activity_sources/native_*.py` are independent
  implementations using only the Python standard library.

## Retained upstream artifacts (pre-existing, under review)

- Root `LICENSE.txt` is the verbatim MPL-2.0 text inherited from the
  ActivityWatch checkout; `CITATION.cff` still cites ActivityWatch.
  Whether these files describe Noema itself or are retained upstream
  artifacts is flagged for review in `docs/licensing/activitywatch.md`.
- `web/LICENSE` is an MIT stub belonging to the dashboard template.
