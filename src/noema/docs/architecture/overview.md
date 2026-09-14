# Noema — Architecture (Generations 0–10)

This package is an independent local activity-intelligence service. It reads
telemetry from a compatible local collector, normalizes events, applies a
privacy policy, and stores derived activity for session and semantic services.

```text
Collector REST/buckets
          ↓  read-only
  ActivityWatchAdapter
          ↓
    EventNormalizer
          ↓
    PrivacyFilter
          ↓
    NormalizedEvent
          ↓
      SQLiteStore
```

Generation 1 adds the session engine:

```text
NormalizedEvent[] → Sessionizer → ActivitySession[] → SQLiteStore
```

The Gen 1.5 Meaningful Session Engine adds a second semantic layer without
replacing `ActivitySession`:

```text
ActivitySession[]
      ↓
continuity scoring (time, transition, topic, project, intent, confidence)
      ↓
MeaningfulSession[]
      ↓
phase detection + evidence + confidence + optional close-time summary
```

Technical sessions remain the evidence. A meaningful session can therefore
span coding, paper reading, search, GitHub, Stack Overflow, and return to
coding when those sessions are semantically continuous. Large gaps lower the
continuity score rather than acting as the only hard boundary. Explicit intent
anchors the grouping, and unsupported outcomes remain `unknown`.

Meaningful sessions are the canonical unit for all downstream generations.
Raw `ActivitySession` records are used to construct and explain them, and are
kept as immutable evidence. Once a meaningful session exists, classification,
intent alignment, behavior, intervention, outcome, memory, and autonomous
recommendation operate on its stable ID.

Generation 2 adds hosted semantic classification with a local fallback:

```text
MeaningfulSession → SemanticClassifier → LLMProvider → SemanticClassification
                                       |
                                 six hosted models
                                       |
                                 Ollama fallback
```

## Quick start

```python
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter, ActivityWatchClient
from noema.api import NoemaApp, create_app, serve
from noema.application.pipeline import NoemaService
from noema.infrastructure.database import SQLiteStore

adapter = ActivityWatchAdapter(ActivityWatchClient())
store = SQLiteStore("noema.db")
service = NoemaService(adapter, store)

# Pull from the configured telemetry source when desired (the adapter performs GET requests only).
service.ingest_telemetry()

# Optional local API: GET /health, GET /api/events, and GET /api/sessions.
serve(service)
```

Sessionization is explicit so a caller can ingest a complete time window and
then persist its sessions:

```python
service.ingest_telemetry(start="2026-09-04T09:00:00Z")
service.sessionize_stored_events(start="2026-09-04T09:00:00Z")
service.classify_stored_sessions(start="2026-09-04T09:00:00Z")  # merger evidence
service.build_meaningful_sessions(start="2026-09-04T09:00:00Z")
service.classify_stored_meaningful_sessions(start="2026-09-04T09:00:00Z")
```

The classifier has one provider boundary. It tries six hosted models in strict
priority order, then contacts local Ollama only as fallback number seven.
Gemini uses the official Google GenAI Interactions API and reads the key only
from the environment variable named by `gemini_api_key_env` (default
`GEMINI_API_KEY`); the key is never written to configuration, SQLite, health,
logs, or browser payloads. A valid ambiguous model result is stored as a
successful classification; provider failures remain retryable.

Provider selection is startup-scoped. The default configuration uses the six
hosted models followed by Ollama. Provider transport/model changes are
intentionally rejected by live config reload and require a daemon restart;
ordinary scheduling and operational settings can still reload in place.

For Windows production startup and crash restart, install the Task Scheduler
definition from an Administrator PowerShell opened in this repository. The
task launches a small supervisor; the supervisor owns the daemon child and
restarts it after an unexpected exit, while the daemon's existing lock handles
stale-PID recovery:

```powershell
python -m noema --install-task-scheduler
python -m noema --query-task-scheduler
```

The task starts at interactive logon after a 30-second delay, ignores a second
concurrent instance, and requests task-level restart-on-failure every 60
seconds as a second safety layer. Verify
the task before disabling the legacy Registry Run entry:

```powershell
python -m noema --uninstall-autostart
```

Only one of the Registry Run entry or Task Scheduler should be active in the
final installation.

Generation 3 captures user intent and exposes deterministic alignment through
`GoalAligner`. Generation 4 turns classifications and alignment into behavior
observations. Generation 5 plans `HOLDOUT`, `NOTIFICATION`, or `MEME`
interventions only for actionable `DISTRACTED` observations, with cooldowns and
dry-run execution enabled by default.

Generation 6 adds meme copy and SVG rendering. Generation 7 records intervention
outcomes and marks recovery when the first later productive session lasts at
least three minutes.

Generation 8 derives validated behavioral memories and intervention preference
profiles. Generation 9 provides portable sync envelopes and a deduplicated
cross-device unified timeline while retaining each event's device identity.

Generation 10 adds a recommendation-first autonomous cycle that composes
ingestion, raw evidence, meaningful sessions, canonical semantics, intent
alignment, behavior, personalization, and intervention planning. It never
bypasses the intervention policy or dry-run boundary.

## Firefox intervention bridge

The optional Firefox WebExtension client is not distributed in this
repository. When present locally (conventionally under
`web/extension/firefox/`), it registers browser identity,
maintains exact current window/tab state, receives intervention action payloads
over WebSocket (with HTTP long-poll fallback), and renders MEME/NOTIFICATION
overlays inside a Shadow DOM. It reports displayed, dismissed, auto-dismissed,
`LOCK_IN`, and `DISMISS_WORKING` states through the local API. HOLDOUT remains a
database-only experimental arm.

Run `serve(service)` to start the local HTTP API and its loopback WebSocket
bridge, then load the extension directory as a temporary add-on in Firefox.

## Continuous daemon

The production-shaped local runtime is started once and then keeps polling
the configured local collector:

```powershell
python -m noema
```

It persists a last-successful-ingestion cursor in SQLite, uses a small overlap
to preserve session boundaries, retries failed stages with exponential
backoff, writes structured JSON logs, and exposes
`GET http://127.0.0.1:8765/api/daemon/health`. The local API and Firefox
WebSocket bridge are started by the same process. The default cadence is
one-second local ingestion, five-minute behavior
evaluation, and twenty-minute semantic classification, alongside the
independent 60-second realtime detection lane. The local fallback model
is `llama3.2:3b`. `--once` and `POST /api/autonomous/cycle` remain available
for an on-demand full cycle. A JSON config can be passed with
`--config path\to\daemon.json`; editing it is picked up automatically,
or immediately with `POST /api/daemon/reload`.

On Windows, install the current-user startup entry once:

```powershell
python -m noema --config path\to\daemon.json --install-autostart
```

The installer uses a per-user Run entry, launches hidden, and preserves the
repository working directory. It is an explicit opt-in operation; the
package does not modify startup settings merely because it is imported.
`--once` is available for a single smoke-test cycle.

For an application-specific policy, pass `PrivacyFilter(PrivacyPolicy(...))`
to `NoemaService`. Blocked events are discarded before SQLite or any
future AI service sees them. Allowed browser URLs have query strings and
fragments removed by default.

The collector adapter is read-only; the intelligence service owns its derived
SQLite records and does not modify the collector.

## Telemetry source boundary

ActivityWatch is an **external, interchangeable telemetry source** — not the
Noema domain. The boundary is exactly one package:

```text
ActivityWatch (buckets: window + afk status)
    ↓  read-only HTTP (ActivityWatchClient)
aw_adapter/  ← the ONLY module that speaks ActivityWatch protocol
    ↓  Noema-owned models
NormalizedEvent (telemetry) + presence state (device truth)
    ↓
Noema domain: normalization → privacy → sessions → classification →
behavior → realtime detection → intervention → outcome
```

Rules:

- Only `aw_adapter/` may import ActivityWatch protocol details (bucket IDs,
  watcher names, `currentwindow` mutability). Downstream code uses
  `NormalizedEvent`, session models, and presence states.
- `bucket_id` is preserved on stored events as source provenance, never as
  domain logic. Presence truth comes from AFK status rows, corroborated by
  bridge freshness — never from window focus.
- Replacing ActivityWatch tomorrow means replacing `aw_adapter/` (plus the
  `telemetry_url` setting). Behavior, classification, intervention,
  outcomes, providers, and the frontend do not depend on it.
- Historical names were canonicalized: `telemetry_url`
  (`AI_ACTIVITY_TELEMETRY_URL`; legacy `AI_ACTIVITY_WATCH_URL` still
  accepted), `ingest_telemetry()`, `source_connected`, `source_today`.
  The historical `collector/` duplicate package was deleted.

## Vocabulary (one concept, one name)

- **Telemetry event** (`NormalizedEvent`): one privacy-filtered heartbeat
  from the source. Never classified, never shown as a verdict.
- **ActivitySession** ("session"): merged heartbeats sharing one context.
  Raw evidence, immutable once persisted.
- **MeaningfulSession** ("meaningful session", "task block"): task-level
  grouping of sessions. The canonical unit for classification, behavior,
  detection, intervention, and outcomes. Stable IDs link everything.
- **Classification** (`SemanticClassification`): a model's verdict on one
  meaningful session (productive/distractive/neutral). Failures stay
  `pending` — pending is never neutral.
- **Presence** (active/afk/unknown): device truth from input timing. Never
  a productivity verdict; AFK vetoes detection and intervention.
- **Behavior state** (`BehaviorObservation`): deterministic engine output
  (FOCUSED/DISTRACTED/DRIFTING/RECOVERING/BREAK/IDLE/NORMAL). Models supply
  evidence; the state machine owns transitions.
- **Detection** (`DetectionRecord`): a persisted real-time decision
  (candidate/verified/intervention), not a classification.
- **Intervention / Outcome**: policy-gated action and its measured
  recovery on later meaningful sessions.

## Observability (metrics, events, benchmarks)

`src/noema/observability/` observes the product; it never decides for
it. Every model call emits one `model_invocations` row (provider, model,
purpose, pipeline, latency, token counts, outcome) plus logical request
rows that tie retries/fallbacks together. Workers, DB stages, and API
requests land in `operation_timings`. Deterministic fixture benchmarks
persist as `benchmark_runs` / `benchmark_results`.

```powershell
python -m noema benchmark full --mock   # offline matrix
python -m noema benchmark full          # live providers
python -m noema metrics summary --days 7
python -m noema metrics prune           # retention only
```

API: `GET /api/metrics/summary|models|providers|tokens|latency|realtime|workers`,
`GET /api/benchmarks`, `GET /api/benchmarks/{id}`. Results & Models shows
model performance, token usage, and benchmark history.

Honesty rules: token counts are EXACT only when the provider reported
them, else ESTIMATED (labeled) or NULL (UNKNOWN, counted never
zero-filled). P95/P99 need n>=5 samples or report `n<5`. Free-tier cost
is explicit FREE with measured tokens; unknown pricing is NULL, never
$0. Retention (`NOEMA_METRICS_RETENTION_DAYS`, default 30)
prunes telemetry tables only. No prompts, responses, keys, or private
activity text are ever stored.
