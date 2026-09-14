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

### Fixed (final public-release validation)

- `CODE_OF_CONDUCT.md` no longer points conduct reports at an upstream
  author's personal email; reports go through a private GitHub security
  advisory (see `SECURITY.md`).
- Removed the `web` gitlink (a submodule-style pointer to an unrelated
  third-party template with no `.gitmodules` entry); `web/` is now an
  ignored local-only workspace and the tracked tree matches the documented
  "bundled dashboard, optional frontend not included" state.
- `src/noema/docs/architecture/overview.md`: Noema product/class names in
  the quick-start (`NoemaService`, `ActivityWatchAdapter`), correct default
  cadences (1s ingest / 5min behavior / 20min semantic + 60s realtime lane),
  and the browser-extension client marked as not distributed.
- `src/noema/docs/CURRENT_SYSTEM_STATUS.md`: hosted inference receives
  privacy-filtered per-session evidence (titles/domains), matching
  `PRIVACY.md` and the classifier code.
- Bundled dashboard greeting no longer hardcodes a personal name.

### Changed (public identity pass)

- `README.md` rewritten around Noema's own identity (Personal Behavioral
  Intelligence; OBSERVE → UNDERSTAND → ALIGN → DETECT DRIFT → INTERVENE →
  MEASURE) with CI/license/Python badges, a Design Principles section, an
  explicit what-ships/what-does-not section, and ActivityWatch covered only
  under "Relationship to ActivityWatch".
- `CONTRIBUTING.md` gained the pipeline layering contract (observed activity
  vs inferred meaning vs intent vs behavioral state vs intervention vs
  outcome), the uncertainty-honesty rule, and per-subsystem contribution
  notes (telemetry, classification, behavior, intervention, privacy).
- `SECURITY.md` rewritten for Noema's actual data handling: precise
  statements about on-device storage, loopback binding, fail-closed privacy
  filtering, minimal hosted-evidence payloads, and report categories
  (telemetry exposure, network transmission, API exposure, credentials,
  browser bridge, database/logging/provider leakage).
- `PRIVACY.md` and code docstrings now describe telemetry sources
  generically (native-first, ActivityWatch optional) instead of
  ActivityWatch-first; `pyproject.toml` keywords lead with
  `behavioral-intelligence`.

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
