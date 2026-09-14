# Noema — Current System Status

> Verified against actual runtime code and test suite (359 passed, 2 skipped).
> Date: 2026-09-13. Branch: `claude/pensive-turing-pg8wop`.

---

## Product Boundary

Noema implements a six-stage continuous loop on a single local machine:

```
OBSERVE → UNDERSTAND → ALIGN → DETECT DRIFT → INTERVENE → MEASURE
```

All processing is local. No data leaves the device except AI API calls (Gemini / OpenRouter), which receive only privacy-filtered per-session evidence — application name, window title/domain evidence for the sessions in the current batch, and the task instruction (see `PRIVACY.md`) — never file contents, keystrokes, screenshots, or full history.

---

## Pipeline Feature Matrix

### 1. OBSERVE — Telemetry Collection

| Feature | Status | Notes |
|---|---|---|
| Noema-native collectors | WORKING | Primary path; `native_enabled=True` default |
| ActivityWatch adapter | WORKING | Legacy compatibility source; optional |
| Privacy filter | WORKING | Fail-closed; strips URL query params; blocks 1password, bitwarden, dashlane, keepass, lastpass, proton pass |
| Private window blocking | WORKING | Any private/incognito window title is always redacted |
| AFK / presence detection | WORKING | 3-minute timeout default; presence-aware sessionization |
| URL query-param stripping | WORKING | Search queries never appear in stored titles |

### 2. UNDERSTAND — Session Building + Classification

| Feature | Status | Notes |
|---|---|---|
| Sessionizer (gap-merge) | WORKING | 15s gap default; presence-aware; AFK splits sessions |
| classification_status field | WORKING | Three values: `pending`, `classified`, `classification_failed` |
| Provider chain | WORKING | OpenRouter → Gemini (6 models) → Ollama (optional tail) |
| Gemini models | WORKING | gemini-3.5-flash-lite (workhorse) through gemini-3.8-flash |
| Batch classification | WORKING | Up to 60 sessions per run; 20-minute cadence |
| Retry with backoff | WORKING | Max 5 attempts; 10/20/40/80/120 min backoff |
| Version-bump re-queue | WORKING | `classification_version` bump re-queues all rows |
| Quota ledger | WORKING | Persistent daily JSON; in-memory per-minute window |
| Quota API | WORKING | `/api/debug/quotas` shows rpm/rpd per model |
| classification/status quota | WORKING | `isLocalEstimate: true`, `quotaScope` present (local estimate, not provider-authoritative) |

### 3. ALIGN — Intent / Goal Tracking

| Feature | Status | Notes |
|---|---|---|
| Intent engine | WORKING | Parses structured model output into user goals |
| Goal aligner | WORKING | Aligns sessions to active intent |
| Semantic episodes | WORKING | Only `classified` sessions contribute evidence (never fabricated) |

### 4. DETECT DRIFT — Behavior Engine + Realtime

| Feature | Status | Notes |
|---|---|---|
| BehaviorEngine states | WORKING | IDLE / BREAK / DISTRACTED / DRIFTING / RECOVERING / FOCUSED / NORMAL |
| Pending/failed exclusion | WORKING | Double-filter: `_candidate()` and `evaluate()` both null non-classified rows |
| Realtime detector | WORKING | 12-signal scorer; independent 1-minute cadence |
| Realtime thresholds | WORKING | enter=0.70, exit=0.50, min_active=120s, cooldown=1800s |
| Semantic scheduler | WORKING | 20-minute cadence; `_semantic_lock` prevents overlap |
| Scheduler/run_once race | FIXED | `run_once()` now holds `_semantic_lock` before `_semantics_stage()` |

### 5. INTERVENE — Intervention Engine

| Feature | Status | Notes |
|---|---|---|
| Intervention modes | WORKING | HOLDOUT / NOTIFICATION / MEME |
| AFK veto | WORKING | Always skips when `presence_state == "afk"` |
| Global cooldown | WORKING | 900s default |
| Per-mode cooldowns | WORKING | MEME=3600s, NOTIFICATION=900s, HOLDOUT=300s |
| Ineffective streak backoff | WORKING | Backs off after 3 consecutive NOT_RECOVERED outcomes |
| EXECUTED ≠ DISPLAYED ≠ SEEN ≠ ACKNOWLEDGED | WORKING | Distinct action records per stage |
| execute_interventions default | WORKING | `DaemonConfig.execute_interventions=True`; CLI always passes it to the engine |
| WebSocket delivery | WORKING | Browser bridge publishes to active tab |
| Desktop notification fallback | WORKING | Fires when no exact browser target |
| dry_run dataclass default | LIMITATION | `InterventionPolicy.dry_run=True` as bare dataclass default; CLI always overrides this correctly via `dry_run=not execute_interventions` |

### 6. MEASURE — Outcome Tracking

| Feature | Status | Notes |
|---|---|---|
| OutcomeTracker | WORKING | Measures recovery = productive session ≥180s post-intervention |
| classification_failed safety | WORKING | Failed rows always have `productivity=neutral`; cannot trigger false recovery |
| Outcome recording | WORKING | RECOVERED / NOT_RECOVERED / INDETERMINATE |

---

## API + Dashboard

| Endpoint | Status | Notes |
|---|---|---|
| `/api/dashboard/recent-activity` | WORKING | Rolling 24h range supported |
| `/api/classification/status` | WORKING | Includes `isLocalEstimate`, `quotaScope` |
| `/api/debug/quotas` | WORKING | Per-model rpm/rpd with `ok` / `exhausted` status |
| `/api/debug/classifier` | WORKING | Debug-only; global scan acceptable |
| CORS | WORKING | Restricted to localhost, loopback, browser extensions only |

---

## Observability

| Area | Status | Notes |
|---|---|---|
| Worker health tracking | WORKING | Per-worker status, run counts, error counts, last result |
| Provider telemetry | WORKING | `TelemetryRecorder` emits invocation events |
| `ollama_*` health keys | STALE NAMES | `ollama_connected`, `last_ollama_analysis`, etc. still used even when Gemini is primary — correct values, misleading names |
| Quota ledger persistence | WORKING | Atomic JSON, survives restart |

---

## Locking / Concurrency

| Lock | Guards | Status |
|---|---|---|
| `_pipeline_lock` (RLock) | `run_once()`, behavior/outcomes/daily_report/realtime workers | WORKING |
| `_semantic_lock` (Lock) | `run_once._semantics_stage()`, semantic background loop, `run_ai_now()` | WORKING (race fixed) |
| `_ingest_lock` (Lock) | Ingest worker exclusively | WORKING |

---

## Database Integrity

| Area | Status | Notes |
|---|---|---|
| PRIMARY KEY / UNIQUE constraints | WORKING | All tables have PK; key tables have UNIQUE guards |
| `INSERT OR IGNORE` for batch_id | WORKING | Prevents duplicate batches |
| UPSERT on classification | WORKING | Full overwrite on conflict (correct: failed→neutral overwrites pending) |
| PROCESSING→RETRYABLE recovery | WORKING | `recover_processing_batches()` on startup |
| FOREIGN KEY enforcement | NOT ENFORCED | `PRAGMA foreign_keys = ON` is set at connection time, but no `FOREIGN KEY ... REFERENCES` clauses exist in the DDL — the PRAGMA enforces nothing. Orphan records are prevented by application-level logic only. |

---

## Privacy

| Feature | Status | Notes |
|---|---|---|
| Fail-closed filter | WORKING | Any filter error → block by default |
| Blocked app list | WORKING | Password managers, Bitwarden, etc. |
| URL query param stripping | WORKING | No search terms in evidence |
| No fabrication | WORKING | Filter only redacts/blocks; never invents data |
| API key security | WORKING | Read from env at request time; never logged or returned |

---

## Known Bugs Fixed (This Session Series)

| # | Bug | Fix |
|---|---|---|
| 1 | `run_once()` held `_pipeline_lock` but not `_semantic_lock` — semantic background loop and `run_once._semantics_stage()` could overlap | Added `with self._semantic_lock:` inside `_semantics_stage()` call in `run_once()` |
| 2 | `ollama_connected` returned `False` for hosted/Gemini providers — health dashboard always showed AI unavailable | Added `"configured"` to the accepted health-value set |
| 3 | Five classification lookup sites used `query_classifications(limit=100000)` global scan instead of targeted per-session queries | Replaced with `query_classification_map()` throughout pipeline |
| 4 | `/api/classification/status` quota field missing `isLocalEstimate` and `quotaScope` — callers had no way to distinguish a local ledger estimate from a provider-authoritative limit | Added both fields to response |

---

## Known Limitations (Not Bugs)

| # | Limitation | Impact |
|---|---|---|
| 1 | `ollama_*` health key names are stale branding (Gemini is primary provider) | Clients reading `ollama_connected` correctly receive AI availability, but the name is confusing. Renaming would be a breaking API change. |
| 2 | `consider_intervention()` does 4 full-table Python-side scans per call | Slow for large databases; not a correctness issue |
| 3 | Schema has `PRAGMA foreign_keys = ON` but no FK clauses in DDL | The PRAGMA is active but enforces nothing. Referential integrity depends entirely on application logic. |
| 4 | `InterventionPolicy.dry_run=True` as bare dataclass default | Safe: CLI always overrides via `_intervention_engine()`. Direct instantiation without CLI would silently dry-run. |
| 5 | No media/audio presence integration | `afk_media_exception` field exists but no browser media-playing signal currently produced |

---

## Test Suite

- **Total:** 359 passed, 2 skipped (as of 2026-09-13)
- **Coverage:** Unit, integration, regression, API, provider, and concurrency tests
- **Regression tests:** 23 passed
- **API tests:** 4 passed (including quota and `isLocalEstimate` regression)

---

## Live Validation

Not performed in this remote CI environment (no ActivityWatch, no Gemini API key, no desktop). All validation is through automated tests against in-memory SQLite stores and mock providers.

---

## Launch Readiness Verdict

**READY WITH KNOWN LIMITATIONS**

The core six-stage pipeline is correctly implemented and tested. The four bugs found during this audit were fixed and covered by regression tests. The known limitations are understood, documented, and do not compromise data integrity or correctness:

- The `ollama_*` naming is stale but the values are accurate.
- The `consider_intervention()` scan performance is acceptable for personal-device usage (single user, bounded history).
- The intervention dry_run default is safe because the CLI always overrides it.

**Recommended before broader release:**
1. Rename `ollama_*` health keys to provider-agnostic names (e.g., `ai_connected`, `last_ai_analysis`) — coordinate with any dashboard clients first.
2. Add targeted SQL queries in `consider_intervention()` to replace the 4 Python-side full-table scans (no store methods exist yet for session/intent/observation lookup by ID).
3. Add `FOREIGN KEY ... REFERENCES` clauses to the DDL to make the existing `PRAGMA foreign_keys = ON` meaningful.
