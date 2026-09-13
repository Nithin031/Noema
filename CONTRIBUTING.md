# Contributing to Noema

Thanks for your interest in Noema — a local-first Personal Behavioral
Intelligence system. Contributions that improve correctness, clarity, tests,
docs, and privacy are very welcome.

## Development setup

Noema is a Python package under `src/noema` with tests under `tests/`.

```bash
# Python 3.9+ (CI runs 3.12)
python -m pip install --upgrade pip

# Editable install with the runtime dependency (google-genai):
pip install -e .

# Or run straight from the source tree without installing:
export PYTHONPATH=src        # PowerShell: $env:PYTHONPATH="src"

# Test tooling:
pip install pytest pytest-cov mypy
```

No API key is required to run the test suite — every provider call is mocked.
A `GEMINI_API_KEY` is only needed to run live hosted classification.

## Checks to run before a PR

```bash
python -m pytest tests -q          # full suite
python -m compileall src/noema -q  # syntax/compile check
mypy src/noema                     # type check (advisory)
```

The test suite must pass. Add tests for every behavioral or correctness
change; regression tests live in `tests/regression/`.

## Frontend

The dashboard served by the daemon is plain static HTML/CSS/JS in
`src/noema/api/dashboard/` and needs **no build step**. An optional richer
React frontend is **not included in this repository**; if you add one under
`web/`, build it with its own tooling into `web/dist/` (the daemon serves
that in preference to the bundled dashboard when present).

## Architecture & where code belongs

Noema follows a layered architecture; please respect the boundaries:

- `domain/` — pure business logic (activity, sessions, meaningful episodes,
  intent/alignment, behavior, intervention, privacy). **No** HTTP, SQLite,
  provider SDKs, or filesystem specifics here.
- `application/` — orchestration/pipelines that compose domain logic.
- `infrastructure/` — adapters that implement I/O: telemetry sources,
  providers, database, browser bridge. Provider-specific behavior stays here.
- `runtime/`, `api/`, `cli/` — integration surfaces (daemon/workers, local
  HTTP API + dashboard, console entry points).
- `observability/` — metrics/benchmarks; observes the product, never decides
  for it.

Do not bypass these boundaries to make a feature easier to implement (for
example, do not reach into SQLite or a provider SDK from the domain layer).

## Privacy is a hard requirement

Noema is local-first and privacy-sensitive. When contributing:

- Never fabricate evidence. Failed classifications stay `pending`/`failed` and
  must never become a synthesized verdict.
- Never weaken the privacy filter or send more than the documented minimal
  evidence to hosted models.
- Never commit `.env`, API keys, databases (`*.sqlite3`), logs, or personal
  activity data. Only synthetic fixtures under `tests/` are published.
- Keep the local API bound to loopback.

See `PRIVACY.md` for the full data-flow policy and `SECURITY.md` for reporting
vulnerabilities.

## Pull requests

- Keep changes focused and explain the motivation.
- Include tests and update docs when behavior changes.
- Preserve `LICENSE.txt`, `CITATION.cff`, and third-party notices.
- Do not introduce new AI providers, subsystems, or architecture without
  discussion first.
