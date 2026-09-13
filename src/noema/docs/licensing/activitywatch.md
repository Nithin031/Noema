# Noema and ActivityWatch

Noema can read telemetry from a local [ActivityWatch](https://github.com/ActivityWatch/activitywatch)
instance, and the project began from an ActivityWatch checkout. This note
records exactly what that relationship is for licensing purposes.

## License

- **ActivityWatch** is licensed under the **Mozilla Public License 2.0
  (MPL-2.0)**.
- **Noema** is also licensed under **MPL-2.0** (see the root `LICENSE.txt`
  and `pyproject.toml`). MPL-2.0 is file-level copyleft: modified MPL files
  must stay MPL and carry their notices, but the license does not extend to
  independent files that merely interoperate.

## What Noema copied or adapted from ActivityWatch

**No ActivityWatch source code is copied or adapted into Noema.**

- The native collectors
  (`src/noema/infrastructure/activity_sources/native_windows.py`,
  `native_presence.py`) are independent implementations using only the
  Python standard library (and an optional `psutil` fast-path with a stdlib
  fallback). They observe the foreground window and input presence directly.
- The ActivityWatch adapter
  (`src/noema/infrastructure/activity_sources/activitywatch/`) is a small,
  **read-only** HTTP client that speaks the ActivityWatch bucket protocol
  over loopback. It consumes ActivityWatch's REST output; it does not
  incorporate ActivityWatch code.

Noema uses ActivityWatch's *protocol and behavior* as an interoperability
reference, which does not create a derivative work of its source.

## Retained upstream artifacts — resolved

- **`LICENSE.txt`** is the standard MPL-2.0 license text. It is the correct
  license for Noema (which is MPL-2.0), independent of its provenance. No
  action needed.
- **`CITATION.cff`** previously carried ActivityWatch's own citation
  metadata (authors, DOI, version). It has been replaced with Noema's own
  citation metadata so that citing "Noema" no longer resolves to
  ActivityWatch. If you wish to also credit ActivityWatch in academic work,
  cite it separately from its upstream repository.
- **`web/LICENSE`** (an MIT dashboard-template stub) referenced by older
  notices is **not present**: this repository does not bundle a web
  frontend. See `THIRD_PARTY_NOTICES.md`.

## If you redistribute Noema

- Keep `LICENSE.txt` and the per-file MPL headers intact.
- Keep `THIRD_PARTY_NOTICES.md` and this file.
- If you add a bundled web frontend or other third-party assets, record
  their licenses in `THIRD_PARTY_NOTICES.md` and
  `dependency-audit.md` before distributing them.
