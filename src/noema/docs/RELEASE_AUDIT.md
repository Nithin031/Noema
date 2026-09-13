# PUBLIC RELEASE AUDIT

> Public open-source hardening pass. Conservative refactor only — no new
> features, no architecture changes, no changes to semantic behavior. The
> repository was left in a clean local state; nothing was pushed, PR'd, or
> merged.

## Release readiness

**READY WITH LIMITATIONS.**

A technically competent developer can now clone the repo, understand what
Noema is, install it (`pip install .`), run the test suite (green), start the
daemon, open the bundled dashboard, and read an accurate privacy/provider/
licensing story. The remaining limitations are documented and are product
scope, not release defects (no bundled web frontend / browser extension; live
end-to-end validation requires a real desktop).

## Repository health

| Area | Status | Notes |
|---|---|---|
| Architecture | GOOD | Clean `domain → application → infrastructure` with `runtime`/`api`/`cli` surfaces. No violations fixed because none material were found. |
| Packaging | GOOD | `pyproject.toml` now has readme, URLs, keywords, classifiers; single version source (`pyproject` ↔ `noema.__version__` = 0.14.0). `pip install .` builds. |
| Documentation | GOOD | README/PRIVACY/FEATURES/ROADMAP accurate after fixes; docs under `src/noema/docs/`. |
| Tests | GOOD | 377 passed / 2 skipped / 0 failed with the declared `google-genai` present. |
| Security | GOOD | No secrets or PII in tree; loopback-only API; fail-closed privacy filter; no stack traces/secrets in API errors. |
| Licensing | GOOD (was a gap) | MPL-2.0 consistent; ActivityWatch relationship documented; dependency audit added; `CITATION.cff` fixed. |
| CI | GOOD (was broken) | Python job installs declared deps so SDK tests run; broken web job removed. |
| Frontend | LIMITATION | Only the bundled static dashboard ships; the optional React app in `web/` is not included. |
| Extension | LIMITATION | Browser-bridge backend ships; the browser-extension client is not included. |

## Files added

- `src/noema/docs/licensing/activitywatch.md` — ActivityWatch relationship & MPL-2.0 note (referenced by notices but previously missing).
- `src/noema/docs/licensing/dependency-audit.md` — per-dependency license table (section 30).
- `src/noema/docs/RELEASE_AUDIT.md` — this report.

## Files modified

- `.github/workflows/test.yml` — install declared `google-genai` (fixes the 2 SDK test failures); removed the web build job (empty `web/`).
- `CITATION.cff` — now cites Noema (was ActivityWatch's upstream citation metadata, incl. foreign authors/DOI/version 0.13.1).
- `README.md` — dashboard is bundled static UI (not "React"), `web/` optional/not-included, Firefox extension client not bundled, semantic cadence ~20 min (was "10 min" ×2), accurate project structure and testing sections.
- `.env.example` — provider default `hosted` (matches code/README, was `ollama`); "~20-minute" scheduler (was "10-minute").
- `.tool-versions` — trimmed to `python` + `poetry` (dropped stale `rust nightly` / `nodejs` inherited from the upstream checkout).
- `THIRD_PARTY_NOTICES.md` — fixed broken `web/LICENSE` and licensing-doc references; links the dependency audit.
- `src/noema/docs/FEATURES.md` — corrected native-collectors path (`infrastructure/native/` → `activity_sources/native_*.py`).
- `pyproject.toml` — `readme`, homepage/repository/documentation URLs, keywords, classifiers.
- `CONTRIBUTING.md` — real dev setup, checks, architecture boundaries, privacy rules (fixed the broken `web` build instructions).
- `CHANGELOG.md` — `[Unreleased]` section for Semantic V2 + this hardening pass; version-source note.

## Files removed

None. (No dead source trees, debris, databases, logs, or secrets were tracked; `logs/` and caches are local-only and already gitignored.)

## Major refactors

None. This pass deliberately touched only docs, metadata, config, and CI. No
Python source logic was modified, so runtime and semantic behavior are
unchanged (verified by the full suite and the 18-test Semantic V2 regression
set).

## Licensing findings

- Noema is **MPL-2.0**, consistent across `LICENSE.txt` and `pyproject.toml`.
- **Fixed:** `CITATION.cff` previously misattributed the entire project to
  ActivityWatch's authors — corrected to Noema.
- **No ActivityWatch code is copied/adapted**; the native collectors are
  independent stdlib implementations and the adapter is a read-only protocol
  client. Documented in `licensing/activitywatch.md`.
- Runtime dependency tree (via `google-genai`, Apache-2.0) is entirely
  permissive (Apache/MIT/BSD/PSF + MPL `certifi`). PyInstaller (GPL-with-
  exception) is a build tool only, not shipped at runtime.

## Security findings

- **No hardcoded secrets, API keys, tokens, or PII** in the working tree
  (scanned source, tests, docs, examples). Keys are read from the environment
  at request time and never logged/persisted/returned.
- Local API binds to `127.0.0.1`; CORS restricted to localhost/loopback/
  extensions; API errors are mapped without stack traces or secret leakage.
- Privacy filter is fail-closed; failed classifications stay `pending`/`failed`
  and never become a fabricated verdict.
- Git history was not rewritten; a deep historical secret sweep was not
  performed in this pass (none present in the working tree).

## Dependency findings

- Exactly **one runtime dependency**: `google-genai` (lazy-imported for the
  hosted provider). `psutil` is an optional dev dep with a stdlib/Win32
  fallback — not required at runtime.
- CI previously installed with `--no-deps`, which omitted `google-genai` and
  caused the two Gemini SDK tests to fail. Fixed.

## Known limitations

- **No bundled web frontend or browser extension.** The daemon serves a
  built-in static dashboard; the richer React app (`web/`) and the browser
  extension client are not part of this repository.
- **Deterministic goal relevance** is lexical (no synonym/paraphrase/acronym
  matching); ambiguous cases resolve to `unknown` by design.
- **Hosted provider dependency** for the default classification path (Gemini);
  quota/rate limits apply; provider failure stays `pending`/`failed`.
- **Native telemetry is Windows-focused**; other platforms rely on the
  ActivityWatch adapter.
- **Live validation not performed** in this environment (no desktop,
  ActivityWatch, or Gemini credentials).

## Tests

Python (with the declared `google-genai` installed):

```
377 passed
2 skipped
0 failed
```

Without `google-genai` (bare container), the two Gemini SDK tests fail at
`from google import genai` only — an environment/provisioning issue the CI fix
resolves. Compile check (`compileall src/noema`) passes. Semantic V2
regression: 18 passed.

Frontend / extension: **N/A** — not included in this repository.

## Remaining blockers

None for a credible source release. Optional pre-tag items:

- Decide whether to publish the web frontend / browser extension, or keep the
  README's "not included" framing.
- If publishing to PyPI, confirm the `Nithin031/Noema` URLs and author
  metadata are final.

## Recommended next step

Tag `0.14.1` (or `0.15.0`) from this hardened state after a maintainer review
of the doc changes, then (per the frozen-architecture guidance) run the
real-desktop end-to-end validation before any further feature work.
