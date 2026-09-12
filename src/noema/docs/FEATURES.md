# Noema feature guide

Deep dive into what each subsystem does, the guarantees it upholds, and
where it lives in code. For the big picture, see `README.md`.

## 1. Ingestion and telemetry sources

**What.** Collectors observe the foreground window, process switches, and
keyboard/mouse input timing, and normalize everything into per-second
`normalized_events`.

**How.** Two source families feed one normalizer:

- *Native collectors* (`src/noema/infrastructure/native/`) — the primary
  path. Window probes, presence probes, and an optional Firefox bridge work
  with ActivityWatch stopped.
- *ActivityWatch adapter*
  (`src/noema/infrastructure/activity_sources/activitywatch/`) —
  intentionally small and read-only; it speaks the existing local telemetry
  protocol and never writes back.

The ingest worker polls every second (`NoemaService.ingest_telemetry`,
`src/noema/application/pipeline.py`), applies the privacy filter
(blocklist + URL sanitization), and stores idempotently — restarts never
duplicate rows.

**Guarantees.** A failing source never blocks the others (composite
isolation); unobservable windows close the span explicitly instead of
fabricating titles; naive clocks are treated as UTC.

## 2. Sessionization (Gen 1)

**What.** Consecutive raw events become `activity_sessions`: one row per
continuous stretch of same-context activity.

**How.** `Sessionizer` (`src/noema/domain/sessions/sessionizer.py`) merges
heartbeats and splits on context change (app/domain/title), AFK gaps, and
merge-gap thresholds (`SESSION_SHORT_EVENT_SECONDS`,
`SESSION_MERGE_GAP_SECONDS`). Browser titles split sessions when the domain
is unavailable, so thin-evidence tabs stay visible instead of being smeared
together. Recomputation is idempotent.

## 3. Meaningful sessions and evidence quality (Gen 1.5)

**What.** Raw sessions merge into *task episodes* — the canonical unit
everything downstream classifies, aligns, and intervenes on.

**How.** `MeaningfulSessionEngine`
(`src/noema/domain/meaningful/engine.py`) merges on temporal proximity plus
a `ContinuityScorer` (`continuity.py`) combining app/title/domain overlap.
When browser domain evidence is missing, an evidence-quality bonus keeps
fragmented tab activity merged instead of shattering into one-second
episodes.

Every episode carries a deterministic quality rating
(`ConfidenceCalculator.assess_evidence_quality`, `confidence.py`):

| Level | Meaning |
|---|---|
| `strong` | Title + domain agree, or title + app + explicit metadata |
| `moderate` | Title + app agree, domain/URL missing or generic |
| `weak` | Only app name or only title; domain unknown |
| `absent` | Nothing beyond a process name |

**Guarantees.** Quality is computed from observations, never by the model.
The model must self-report quality in its output, but the stored rating is
the deterministic one. API: `GET /api/meaningful-sessions`
(`MeaningfulSession.to_dict` includes `evidence_quality`).

## 4. Classification

**What.** Each episode gets one verdict: `productive / distractive /
neutral`, activity type, confidence, and a cited signal.

**How.** `Classifier` (`src/noema/application/classification.py`) builds a
JSON evidence payload (episode + raw-session context + previous sessions),
packs sessions into token-budgeted groups, and calls the provider chain in
batch (`classify_many` → `ProviderChain.classify_batch`). Oversized evidence
is chunked and synthesized, never truncated silently.

The prompt enforces three critical rules: thin evidence → `neutral`
(0.0–0.59), never `distractive`; `neutral` never auto-becomes distraction;
failures stay `pending`/`failed`.

**Guarantees.** No fabricated domains/URLs. No confidence inflation. No
fallback labels — every provider error keeps the session pending with retry
state (`retry_count`, `next_retry_at`, terminal after
`NOEMA_CLASSIFICATION_MAX_RETRIES`). API: `GET /api/classifications`,
`POST /api/ai/run`.

## 5. Provider chain and quotas

**What.** Ordered model failover with a persisted per-model quota ledger.

**How.** `ProviderChain` (`src/noema/infrastructure/providers.py`) tries
providers in strict priority order with per-model RPM/TPM/RPD guards,
cooldowns, and a daily ledger (`noema.usage.json`) that survives restarts.
Default deployment is Gemini-only (six ranked Flash models); the OpenRouter
free tier and local Ollama paths exist in code and are config-gated.

**Guarantees.** Unknown quota/token values are reported as unknown, never
zero. Secrets live only in environment variables and never reach telemetry,
quotas, logs, or the browser. API: `GET /api/debug/quotas`,
`GET /api/classification/status`.

## 6. Behavior engine

**What.** Episodes + verdicts roll up into `FOCUSED / DISTRACTED / IDLE`
observations with focus and distraction scores.

**How.** `BehaviorEngine.evaluate` runs over meaningful sessions with their
classifications and intent alignments
(`NoemaService.evaluate_stored_behavior`). The daemon re-evaluates on a
5-minute cadence. API: `GET /api/behavior`.

## 7. Interventions, policy, and outcomes

**What.** Acting on sustained distraction — carefully.

**How.** `InterventionEngine` + `InterventionPolicy` enforce per-mode
cooldowns, repeated-ineffective de-escalation (meme → notification), AFK
veto, and recovery detection. `consider_intervention` only fires on an
actionable observation with no already-handled intervention for the session.
`OutcomeTracker` measures recovery afterwards. Modes: notification, meme
(SVG renderer + meme intelligence), browser holdout, exact-tab actions via
the Firefox bridge.

**Guarantees.** Classification alone can never trigger an action. AFK never
counts as recovery. API: `GET /api/interventions`, `GET /api/outcomes`.

## 8. Realtime lane

**What.** Minute-scale drift detection, independent of the 20-minute
semantic scheduler.

**How.** Every 60s: rolling 60-minute behavior windows → local candidate
score → hysteresis (enter/exit thresholds, no flapping) → fast-model
verification **only on entry** → intervention only on confirmed verdict +
existing behavior evidence. The verifier fails safe to `concerning=false`;
a failing verifier never breaks evaluation. API:
`GET /api/realtime/status`, `POST /api/realtime/evaluate`,
`GET /api/distraction/fast-check` (advisory, read-only).

## 9. Presence and AFK

**What.** Ground truth about whether the human is there.

**How.** Input-timing telemetry only — window focus is never presence.
`PresenceDetector` builds AFK intervals; idle time is excluded from
classification and forms its own `afk` timeline bucket. Sleep gaps yield
`unknown`, never presence. Media exceptions are inert without a producer.
API: `GET /api/presence/current`.

## 10. Intents and alignment

**What.** The user's stated goal, so activity can be judged against intent,
not just generic productivity.

**How.** `IntentEngine.capture` structures free text locally;
`GoalAligner` scores session↔intent alignment with explainable,
persistable results. API: `POST /api/intents`, `POST /api/align`.

## 11. Dashboard

**What.** Local-first React UI served by the daemon itself — no separate
hosting, no build step for the user.

**How.** `web/` (Vite + React 19 + Tailwind) builds to `web/dist/`, which
`NoemaApp` serves at `/` with no-cache headers. Views: command center
(activity stream with verdict + evidence badges, pipeline queue, quotas),
results & models, realtime, behavior. A dependency-free legacy dashboard
(`src/noema/api/dashboard/`) remains as fallback. The frontend polls every
30s and never reinterprets backend state.

## 12. Observability and benchmarks

**What.** Proof the system does what it claims.

**How.** Every model call records purpose, pipeline, tokens (exact vs
estimated vs unknown), latency, retries, and fallback depth
(`src/noema/observability/`). The benchmark harness runs fixture sets per
model with schema validation and p50/p95 reporting, persisted across
restarts. CLI: `python -m noema benchmark full --mock`,
`python -m noema metrics summary`. API: `/api/metrics/*`, `/api/benchmarks`.
Retention pruning touches observability tables only — product data is never
pruned.
