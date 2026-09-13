# Changelog

All notable changes to Noema are documented here. This project adheres to
[Semantic Versioning](https://semver.org/); the single source of truth for
the version is `pyproject.toml` (mirrored by `noema.__version__`).

## [Unreleased]

### Added

- **Semantic V2 — three-valued goal alignment.** Goal *relevance* is now
  separated from the raw semantic *category*. `AlignmentResult` gains
  `relation` (`aligned` / `misaligned` / `unknown`), `goal_relevance`
  (`high` / `medium` / `low` / `none` / `unknown`), and
  `alignment_confidence`, with an `alignments` table migration and a
  non-fabricating backfill of legacy rows. Behavior drifts only on an
  explicit `misaligned` verdict; uncertainty (`unknown`) never drifts.
- **Conservative deterministic relevance (audit fixes).** Productivity/verb
  bonuses can no longer manufacture `aligned` without genuine topical
  overlap; `misaligned` requires positive distractive evidence. Insufficient
  evidence stays `unknown`. See `src/noema/docs/SEMANTIC_V2_RELEVANCE_AUDIT.md`
  and `SEMANTIC_V2_STATUS.md`.
- Classifier cache identity now includes prompt and classifier versions, so a
  version bump never reuses a stale in-memory verdict.
- Episode lineage (`meaningful_session_id`) exposed on recent-activity rows so
  a single episode verdict is not shown as many independent judgments.
- Licensing docs: `src/noema/docs/licensing/activitywatch.md` and
  `dependency-audit.md`.

### Changed (public-release hardening)

- `CITATION.cff` now describes Noema (previously carried ActivityWatch's
  upstream citation metadata).
- Documentation corrected to match the code: ~20-minute semantic cadence
  (was "10-minute" in README/`.env.example`), provider default `hosted`, the
  built-in dashboard is bundled static HTML/CSS/JS (the optional React
  frontend and the browser-extension client are **not** included in the
  repository), and the `infrastructure/native/` path in `FEATURES.md`.
- `pyproject.toml` metadata: `readme`, repository/homepage/documentation
  URLs, keywords, and trove classifiers.
- CI installs the declared `google-genai` dependency so the Gemini SDK tests
  run; the web build job was removed (no frontend is bundled).
- `.tool-versions` trimmed to Python + Poetry (dropped stale Rust/Node
  entries inherited from the upstream checkout).
- `THIRD_PARTY_NOTICES.md` corrected (removed the broken `web/LICENSE`
  reference; fixed licensing-doc paths) and `CONTRIBUTING.md` expanded with
  setup, checks, architecture boundaries, and privacy rules.

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
