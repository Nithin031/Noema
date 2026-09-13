# Dependency license audit

Scope: dependencies declared in `pyproject.toml` and resolved in
`poetry.lock`. Licenses are the SPDX identifiers each project commonly
publishes; where a value could not be confidently established from
repository/package metadata it is marked **UNKNOWN**, not guessed.

Noema itself is **MPL-2.0**. Its distributable profile is permissive: the
only runtime dependency is `google-genai` (Apache-2.0), which pulls a
standard permissive transitive set. No GPL/strong-copyleft code ships in the
runtime package.

## Direct runtime dependency

| Dependency | Purpose | Source | License | Distribution implication | Notice required? | Status |
|---|---|---|---|---|---|---|
| google-genai | Hosted (Gemini) semantic classification SDK | PyPI | Apache-2.0 | Permissive; include license/NOTICE if redistributing the package | Yes (Apache NOTICE) | OK |

Only imported when the hosted/Gemini provider is active (lazy import). No API
key is embedded; keys are read from the environment at request time.

## Direct development / build dependencies (not shipped at runtime)

| Dependency | Purpose | License | Notes | Status |
|---|---|---|---|---|
| pytest | Test runner | MIT | dev only | OK |
| pytest-cov | Coverage | MIT | dev only | OK |
| pytest-benchmark | Benchmarks | BSD-2-Clause | dev only | OK |
| mypy | Type checking | MIT | dev only | OK |
| psutil | Optional PID-liveness fast path (stdlib fallback exists) | BSD-3-Clause | dev only; runtime works without it | OK |
| setuptools | Build/runtime shim for PyInstaller | MIT | pinned `<81` | OK |
| pyinstaller | Optional standalone-binary packaging | GPL-2.0-or-later **with bootloader exception** | Build tool, not distributed with the package; the exception permits shipping built apps under any license | OK (build-only) |
| pyinstaller-hooks-contrib | PyInstaller hooks | Apache-2.0 / GPL-2.0-with-exception | Build tool only | OK (build-only) |
| pywin32-ctypes | Windows packaging support | BSD-3-Clause | win32 only, dev | OK |
| pefile | Windows packaging support | MIT | win32 only, dev | OK |

## Notable transitive dependencies (via google-genai)

Resolved at install time by pip/poetry; not vendored into this repository.
All are permissive. Verify exact identifiers against each pinned version's
metadata before a formal distribution.

| Dependency | License (as commonly published) |
|---|---|
| requests, tenacity, google-auth, cryptography | Apache-2.0 |
| certifi | MPL-2.0 (file-level copyleft; ships its own bundle + license) |
| httpx, httpcore, h11, websockets, idna, pyasn1, psutil | BSD |
| pydantic, pydantic-core, urllib3, charset-normalizer, anyio, sniffio, cffi, annotated-types | MIT |
| typing-extensions | PSF-2.0 |

## Findings

- **No strong-copyleft runtime code.** The runtime tree is Apache/MIT/BSD/PSF
  plus MPL-2.0 (Noema itself and `certifi`), all of which permit
  redistribution with attribution and preservation of notices.
- **PyInstaller (GPL-with-exception)** is a build tool only. It is not a
  runtime dependency and is not distributed as part of the Noema package;
  its bootloader exception additionally allows distributing built binaries
  under any license.
- **ActivityWatch** is not a code dependency — see `activitywatch.md`. Noema
  only speaks its local REST protocol.
- No third-party fonts, icons, or media assets are bundled in this
  repository (the optional web frontend and browser extension are not
  included). If such assets are added, extend this table before distributing
  them.
