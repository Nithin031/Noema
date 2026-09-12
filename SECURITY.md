# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.14.x  | :white_check_mark: |
| < 0.14  | :x: |

## Reporting a Vulnerability

Noema is local-first: the daemon binds to loopback only and never
exfiltrates activity data. If you discover a vulnerability (credential
leakage, secret in logs/telemetry, SSRF beyond the configured local
telemetry source, auth bypass on the local API, or private data sent to
a hosted model beyond the documented minimal classification payload),
please open a **private security advisory** on GitHub or contact the
maintainers through the repository's published contact.

Include: affected version, reproduction steps, expected vs actual
behavior, and whether any secret or personal data is involved. Do not
include real API keys, tokens, or personal activity data in the report.

We aim to acknowledge within 72 hours and to ship a fix or mitigation
promptly. Secrets or personal data accidentally committed should be
reported the same way so they can be purged from history.
