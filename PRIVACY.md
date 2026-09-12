# Privacy Policy

Noema is local-first Personal Behavioral Intelligence.

## What stays local

- Raw ActivityWatch events are read through a read-only local adapter.
- The Noema SQLite database (`noema.sqlite3`) lives on your machine
  (default `%LOCALAPPDATA%\Noema` on Windows, `./noema.sqlite3` otherwise).
- Sessions, meaningful sessions, presence, interventions, outcomes,
  benchmarks, and quota ledgers never leave the machine.
- `.env` (API keys), `*.sqlite3`, and `logs/` are gitignored and are
  never committed.

## What is sent to hosted models

Only when hosted classification is enabled, and only the minimal
evidence needed for one compact classification or verification prompt:

- application name, window title/domain evidence for the sessions in
  the current batch, and the short task instruction.
- No full history, no file contents, no keystrokes, no screenshots.
- Realtime verification sends only a compact behavioral summary
  (durations, ratios, switch counts), never raw event streams.
- API keys are sent only as `Authorization: Bearer` to the configured
  provider endpoint; they are never written to logs, the database,
  telemetry, or the browser.

## What is never collected

- No analytics, no telemetry beacons, no remote crash reports.
- The local API binds to `127.0.0.1` by default.
- Failed classifications stay `pending`/`failed` and are never
  silently relabeled.

## Your controls

- `NOEMA_PROVIDER=ollama` plus `NOEMA_OLLAMA_ENABLED=true` keeps all
  inference on-device (Ollama at `http://127.0.0.1:11434`).
- Set `OPENROUTER_ENABLED=false` and unset `GEMINI_API_KEY` to disable
  hosted tiers entirely; the chain then serves locally or fails
  honestly with work left pending.
- Delete `noema.sqlite3` (and the adjacent `.usage.json` ledger) to
  erase derived data. Exported ActivityWatch data, screenshots, and
  personal logs must never be committed to the public repository;
  only synthetic fixtures under `tests/` are published.
