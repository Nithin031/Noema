# Classification Extraction — Baseline (Phase 2, Step 0)

Recorded before any extraction modification. All values below are the
pre-extraction behavior that every later step must preserve exactly.

(NOTE: this document lives under `src/noema/docs/classification/` rather
than a repo-root `docs/` directory because the root `docs/` path is
gitignored legacy. Location is documentation-only; it changes nothing.)

## Repository state

- HEAD: `dea51759d306bcf3a8fc399987e28529ed979af4` ("OLD UI")
- Working tree at record time: 12 modified + 4 untracked paths (pending
  classification/Meme Center work landed separately; see git log).
- Python: 3.12.10 (Windows), repo source via `PYTHONPATH=D:\Noema\src`.
- Node toolchain: per `web/package.json` (Vite 8, React 19, vitest 5).

## Backend baseline

- Command: `pytest tests -q`
- Result: **520 passed**, 1 pre-existing warning
  (`tests/providers/test_openrouter.py::test_key` return-value notice;
  untouched by extraction).
- Previous full-suite baselines in this project: 478 → 481 → 501 → 510 →
  520 as member-batch, semantic-gate, and batch-hardening work landed.
  Every step was green before its commit.

## Frontend baseline

- `npx vitest run`: **29 passed** (3 files).
- `npm run lint` (`--max-warnings 0`): clean.
- `npx tsc --noEmit`: clean.
- `npm run build`: clean (pre-existing chunk-size warning only).

## Classification configuration (non-secret values only)

- Classification tier: `gemini-3.8-flash → gemini-3.7-flash → gemini-3.6-flash`
- Reasoning tier: `gemini-3.5-flash → gemini-3.5-flash-lite → gemini-3.1-flash-lite → gemini-2.5-flash`
- `PROMPT_VERSION = "2"`, `CLASSIFIER_VERSION = "1"`.
- Scheduler cadence 20 min; `max_per_run` 60 episodes; member cap 20/episode;
  batch token budget 200000; output reserve 500/item.
- No API keys are recorded in this document. Provider credentials live only
  in the local untracked `.env`.

## Behavioral pins (must hold after every step)

- One verdict per classified unit; member rows keyed by own session id.
- Batch transport with per-item `session_id` echo; duplicates fail the group.
- Quota guards on batch AND single paths; one served group = one ledger request.
- Inheritance/extension/provenance display rules in the dashboard.
- terminally-failed / backoff / version / AFK eligibility semantics.
