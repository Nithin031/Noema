# Noema roadmap — future improvements

Proposals, not promises. Ordered roughly by value per effort. Anything here
should preserve the standing guarantees: never fabricate evidence, failures
stay pending, classification alone never triggers action.

## Near term

- **Native browser URL capture.** Firefox evidence is `weak` without the
  extension bridge (no domain/URL). Capture the active tab URL natively via
  OS accessibility APIs (UI Automation on Windows) so episodes reach
  `moderate`/`strong` with zero setup.
- **Re-enable a curated OpenRouter tier.** The tier ships disabled after
  unreliable free models (Gemma 4, Nemotron Super/Ultra) burned quota with
  429/502s. Re-enable only with an allowlist (currently just the Nemotron
  Nano reasoning model), per-model circuit breakers, and the shared-pool
  guards already in `provider_quotas`.
- **Verify local-only mode end to end.** `NOEMA_PROVIDER=ollama` exists but
  the recent focus has been the hosted chain. A clean-room run (keys unset)
  should prove classification, realtime verification, and intent capture all
  work fully offline.
- **Nightly live-provider CI.** Public CI uses mocks only. A scheduled
  workflow with secrets should classify a frozen fixture set against the
  real chain and alert on schema drift, quality regression, or quota-shape
  changes.
- **Timeline search and filters in the dashboard.** Full-text search across
  titles/domains/signals plus saved filters (e.g. "all weak-evidence
  distractives this week").

## Mid term

- **Embedding-assisted continuity.** `embed_chunks` and the quota plumbing
  for embeddings already exist. Use title embeddings as an additional
  continuity signal so topically continuous work merges even when app/title
  strings differ (e.g. docs → editor → terminal in one task).
- **Benchmark-driven model selection.** The harness records per-model
  quality/latency/cost. Promote the chain order from static config to
  automatic: reorder (or pin) models from recent benchmark deltas, with the
  choice surfaced in the UI.
- **Calendar-aware intents.** Pull work blocks from a local calendar (ICS)
  as candidate intents so alignment starts from the day's plan instead of a
  blank prompt. Manual intent entry stays as override.
- **Richer interventions.** More notification channels (Action Center
  actions), intervention templates per activity type, and user feedback
  ("helpful / not helpful") feeding the policy's ineffectiveness streaks.
- **Data management UI.** Retention controls, per-day export (JSON/CSV),
  and one-click purge with the same backup-first discipline as
  `rerun_inference.py`.

## Research

- **Cross-device timelines.** Memory-sync primitives
  (validation, dedupe, round-trip) are tested but unwired to a transport.
  Design decision needed: local-network sync vs. end-to-end-encrypted relay,
  with device identity and conflict rules settled before any code ships.
- **Distraction-pattern learning.** `memories` currently store derived
  patterns; close the loop by turning repeated patterns into personalized
  detector priors (per-user enter/exit threshold adaptation) with explicit
  on/off and full reset.
- **Proactive planning.** A morning brief generated from yesterday's
  episodes + today's calendar: predicted focus blocks, likely distractors,
  suggested intents. Strictly advisory — never auto-sets intent.
- **Paid-tier cost tracking.** Quota accounting assumes free tiers. If paid
  keys are used, record real per-call cost from provider responses and add
  budget caps with hard stops.

## Non-goals (deliberate)

- Cloud accounts, hosted dashboards, or any telemetry leaving the machine
  by default.
- Automatic actions without policy gates — the human stays in the loop.
- Inferring presence from cameras, microphones, or network traffic.
- Selling, sharing, or training external models on user data.
