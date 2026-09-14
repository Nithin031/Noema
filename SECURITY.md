# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.14.x  | :white_check_mark: |
| < 0.14  | :x: |

## Why Noema security reports matter

Noema observes computer activity — foreground applications, window titles,
browser context, and input presence. That makes privacy and security handling
unusually important, and unusually worth reporting precisely. Please read what
the implementation actually does before reporting, so reports can be triaged
accurately.

What the implementation does today:

- Raw telemetry, the SQLite database (`noema.sqlite3`), logs, quota ledgers,
  and configuration stay on the local machine; the daemon's HTTP API and
  browser bridge bind to loopback only.
- A fail-closed privacy filter redacts or blocks sensitive evidence (password
  managers, private windows, URL query strings/fragments) before persistence
  or inference — but window titles and domains that pass the filter **are**
  stored locally and, when hosted inference is enabled, compact
  per-session evidence **is** sent to the configured model provider.
- API keys are read from the environment at request time and are never written
  to logs, the database, telemetry, or browser payloads.
- Failed classifications stay `pending`/`failed`; the system never fabricates
  a verdict to fill a gap.

## What to report

Reports in any of these areas are especially welcome:

- **Telemetry exposure** — activity data reachable beyond the local machine or
  local user account (files, logs, backups, error payloads).
- **Unintended network transmission** — anything leaving the machine besides
  the documented minimal classification/verification payloads and the
  provider `Authorization` header.
- **Local API exposure** — the API or browser bridge reachable off-loopback,
  CORS or origin checks bypassable, unauthenticated control endpoints
  performing sensitive actions.
- **Credential handling** — API keys or tokens logged, persisted, returned by
  the API, or embedded in committed files.
- **Browser integration** — the bridge accepting events from unexpected
  origins, or intervention payloads leaking data to page contexts.
- **Local database exposure** — world-readable database/ledger paths,
  unprotected backups, or exports containing more than documented.
- **Sensitive logging** — titles, URLs, query strings, keys, or tokens in
  log output.
- **Provider data leakage** — prompts carrying more than the documented
  minimal per-session evidence (see `PRIVACY.md`).

## How to report

Please open a **private security advisory** on the Noema GitHub repository
(see the repository's Security tab). Include: affected version, reproduction
steps, expected vs. actual behavior, and whether any secret or personal data is
involved. **Do not include real API keys, tokens, or personal activity data in
the report** — describe them redacted (e.g. "a window title containing a
password-manager vault name appeared in …").

Secrets or personal data accidentally committed should be reported the same way
so they can be purged from history.

We aim to acknowledge within 72 hours and to ship a fix or mitigation promptly.
