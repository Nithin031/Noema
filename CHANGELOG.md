# Changelog

All notable changes to Noema are documented here.

## [0.14.0] - 2026-09-11

### Added

- Installable `noema` package (`pip install .`, `python -m noema`,
  `noema` console script) with version aligned to `0.14.0`.
- Safe API error mapping: source-unavailable → 503, database
  → 503, validation → 400, unknown → 404, internal → 500 with no
  stack traces or secret leakage (`tests/api/test_error_mapping.py`).
- `PRIVACY.md`, `CHANGELOG.md`, Noema `SECURITY.md`, and public CI
  (`.github/workflows/test.yml`) using mocks only (no keys, no
  ActivityWatch, no Ollama).

### Fixed

- Stale `FocusForge` / `ai_activity_os` / `AI Activity OS` branding in
  user-visible strings, notifications, scheduler metadata, default DB
  path (`%LOCALAPPDATA%\Noema`), docs, and package authors.
  `AI_ACTIVITY_OS_*` environment names remain accepted as deprecated
  aliases (canonical `NOEMA_*` wins).
- `pyproject.toml` package discovery (`packages`, `scripts`) so clean
  installs work without `PYTHONPATH` hacks.

### Verified

- 268 tests pass (`py -m pytest tests -q`).
- Frontend `tsc && vite build` and `eslint --max-warnings 0` pass.
- Live OpenRouter / Gemini / Ollama (generate + embeddings) verified;
  chain fallback (OpenRouter → Gemini → Ollama → honest pending)
  verified under transient 503s.
- 50k-event realistic benchmark: bulk ingest ~2.3–3.7s, sessionize
  ~2.0s, meaningful ~0.15s — no regression vs reference.
- 38-route API sweep: empty-state 200s, daemon-gated 503s, param-gated
  400s, unknown 404s, no stacks or secrets in responses.
