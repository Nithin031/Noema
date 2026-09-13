# Noema — Semantic Intelligence V2 Status

> Scope of this change: goal **relevance** and **alignment** are now first-class,
> three-valued, and separate from the raw semantic category. Uncertainty can no
> longer masquerade as distraction — neither as a false ALIGNED that hides drift
> nor as a false MISALIGNED that invents it (see the relevance audit fixes F1/F2
> in §E and `SEMANTIC_V2_RELEVANCE_AUDIT.md`).
> Verified against the test suite: **377 passed, 2 skipped, 0 failed** with the
> repo-declared `google-genai` installed; **375 / 2 / 2** without it, where the 2
> failures are environmental only (`google-genai is not installed`) and proven
> identical to the pre-change baseline commit `78a9101`. Date: 2026-09-13.
> Branch: `claude/serene-mccarthy-9n4kax`.

This document extends, and does not replace, `CURRENT_SYSTEM_STATUS.md`. The
telemetry, sessionization, classification, behavior, realtime, intervention,
and outcome pipelines described there are unchanged except where called out
below.

---

## A. Architecture

The semantic layer keeps every stage as a distinct object; none are collapsed.

```
RAW TELEMETRY            NormalizedEvent (privacy-filtered heartbeat)
      ↓
ACTIVITY SESSION         ActivitySession (merged heartbeats; immutable evidence)
      ↓
MEANINGFUL EPISODE       MeaningfulSession (canonical unit; stable ID)
      ↓
SEMANTIC ACTIVITY        Classification (category + confidence + evidence_quality)
      ↓                  ── goal-independent: "what is the user doing?"
ACTIVE GOAL              Intent (user-entered goal + tokens)
      ↓
GOAL RELEVANCE           AlignmentResult.goal_relevance (high/medium/low/none/unknown)
      ↓
ALIGNMENT                AlignmentResult.relation (aligned/misaligned/unknown)
      ↓
BEHAVIOR                 BehaviorObservation (FOCUSED/DRIFTING/DISTRACTED/…)
      ↓
INTERVENTION             Intervention (policy-gated; cooldown/AFK/hysteresis)
```

Key architectural decisions:

- **Classification is goal-independent.** A `Classification` answers "what is the
  user doing?" using observed evidence only. It never encodes the goal. This is
  why the classification cache key excludes goal context (see §F, §Cache).
- **Alignment is deterministic and inspectable**, computed by `GoalAligner`, not
  by the model. No model output can trigger an action. This keeps §24
  (intervention safety) intact: the model never decides distraction.
- **Relevance and alignment are separate.** "Productive in general" and "aligned
  with my current goal" are different questions with different answers.

---

## B. What changed

| File | Change |
|---|---|
| `src/noema/domain/intent/alignment.py` | Rewrote `AlignmentResult` to carry three-valued `relation`, separate `goal_relevance`, and `alignment_confidence` (backward-compatible defaults). Rewrote `GoalAligner.align` so insufficient evidence → `unknown` (never `misaligned`), `misaligned` requires a confident well-evidenced *classified* verdict, and stop words no longer fake goal overlap. Added `AlignmentResult.unknown(...)` and relation/relevance constants. |
| `src/noema/domain/intent/__init__.py` | Exported the new relation/relevance constants. |
| `src/noema/domain/behavior/engine.py` | `DRIFTING` now requires `relation == "misaligned"` instead of `not aligned`. Uncertainty (`unknown`, no goal, legacy `aligned=False`) no longer drifts. |
| `src/noema/infrastructure/database/store.py` | Added `relation`, `goal_relevance`, `alignment_confidence` columns to the `alignments` table (DDL + migration). Insert/read the new fields. Idempotent, non-fabricating backfill of legacy rows. |
| `src/noema/application/classification.py` | Added `CLASSIFIER_VERSION`; `Classifier._cache_key` now includes `prompt_version` + `classifier_version` so a version bump never reuses a stale in-memory verdict. |
| `src/noema/application/pipeline.py` | `recent_activity` now emits `meaningful_session_id` on every raw row, making the raw → episode → classification lineage explicit alongside the existing `inherited_from`. |
| `tests/regression/test_semantic_v2.py` | New: 16 regression tests (see §H). |
| `src/noema/docs/SEMANTIC_V2_STATUS.md` | This report. |

No public API was removed. `AlignmentResult`'s six-positional constructor and
`to_dict()` remain valid; new fields are additive.

---

## C. Data model (actual repository terminology)

| Object | Where | Notes |
|---|---|---|
| `NormalizedEvent` | `domain/activity` | Privacy-filtered telemetry heartbeat. |
| `ActivitySession` | `domain/sessions` | Merged heartbeats; immutable raw evidence. |
| `MeaningfulSession` | `domain/meaningful/models.py` | Canonical episode; stable ID; `evidence_quality`. |
| `Classification` | `application/classification.py` | `category` ∈ {productive, distractive, neutral}; `productivity`; `confidence`; `evidence_quality`; `classification_status` ∈ {pending, classified, classification_failed}; `provider`/`model`/`source`; `prompt_version`/`classifier_version`. |
| `Intent` | `domain/intent/engine.py` | The active goal: `text`, `goal`, `topic`, `project`, `keywords`, `created_at`, `id`. |
| `AlignmentResult` | `domain/intent/alignment.py` | `relation` (aligned/misaligned/unknown), `goal_relevance` (high/medium/low/none/unknown), `alignment_confidence`, plus legacy `aligned`/`score`/`confidence`/`reason`. |
| `BehaviorObservation` | `domain/behavior/models.py` | Deterministic state; `distraction_score`; `actionable`. |
| `Intervention` / `intervention_outcomes` | `domain/intervention`, store | Policy-gated action and measured recovery. |

**Goal model.** The authoritative active goal is the most recent `Intent`
(`query_intents(limit=1)`), as the runtime already selected it. This change does
**not** auto-create goals from telemetry, does not hallucinate goals, and does
not add a parallel goal system. If no `Intent` exists, alignment is `unknown`.

---

## D. Classification policy

`Classification.category` is unchanged and remains goal-independent:

- **productive** — evidence shows work/research/study/learning/professional comms.
- **distractive** — explicit evidence of entertainment/gaming/casual scrolling, or
  a configured policy prior supported by observed telemetry.
- **neutral** — the activity is understood but not confidently categorizable, or
  evidence is too thin (confidence 0.0–0.59). Neutral is *not* distraction.
- Failures (invalid model output, provider error) stay
  `pending` / `classification_failed` — **never neutral-as-verdict**.

Evidence rules (enforced by the prompt and the deterministic evidence layer):
missing domains/URLs/queries/contents are never fabricated; thin evidence caps
confidence and stays neutral; titles are high-value evidence; browsers, search,
YouTube, communication, and games are contextual, not inherently anything.

---

## E. Goal relevance (how it is determined)

`GoalAligner.align(session, intent, classification)` is deterministic:

1. Tokenize the intent (goal/topic/project/keywords) and the session
   (title/app/domain + classification topic/project/category/activity_type).
   **Stop words are removed from both sides** so a shared "for"/"the" cannot fake
   a relationship.
2. `overlap_score = |overlap| / |intent_tokens|`. Bonuses only apply **when
   there is genuine overlap** (audit fix F1): with overlap,
   `score = min(1, overlap_score·0.60 + productivity_bonus(0.15) +
   verb_compatibility_bonus(0.25))`; with no overlap, `score = 0`.
3. Decide (threshold 0.35):
   - `score ≥ 0.35` → **aligned**; relevance **high** (`score ≥ 0.6`) else **medium**.
   - `score < 0.35` but some overlap → **unknown**; relevance **low** (borderline).
   - no overlap **and** a *distractive* verdict (category `distractive` or
     productivity `distracting`) that is confident and well-evidenced
     (`confidence ≥ 0.5`, `evidence_quality ∈ {strong, moderate}`) → **misaligned**;
     relevance **none** (audit fix F2).
   - everything else (no overlap, or productive-but-unmatched, or thin/failed
     evidence) → **unknown**; relevance **unknown**.

Consequences (all covered by tests):

- **Productive ≠ aligned.** A productive activity with no goal overlap is never
  aligned; it is `unknown` (it may be relevant but worded differently, so we do
  not guess).
- **Bonuses cannot manufacture alignment** (F1). "Productive work whose type
  matches a goal verb" with zero topical overlap is `unknown`, not a false
  `aligned` that would hide drift.
- **Distractive ≠ misaligned without evidence.** A distractive-looking activity
  with thin evidence is `unknown`.
- **Misaligned needs positive evidence of unrelatedness** (F2): a confident,
  well-evidenced *distractive* verdict with no goal overlap. Confident
  *productive* work that merely didn't share a token stays `unknown`, so
  relevant-but-differently-worded work (YOLO11 docs for a YOLO goal; a MuJoCo
  quadruped sim for a Unitree A1 goal) never produces **false drift**.
- **No goal → unknown.** Callers pass no alignment (or `AlignmentResult.unknown`).

---

## F. Provenance

Every verdict remains traceable:

| Dimension | Field | Source |
|---|---|---|
| Provider | `Classification.provider` / `source` | transport boundary (never trusted from payload) |
| Model | `Classification.model` | provider chain (`last_model`) |
| Classifier version | `Classification.classifier_version`, module `CLASSIFIER_VERSION` | code contract |
| Prompt version | `Classification.prompt_version`, module `PROMPT_VERSION` | prompt contract |
| Goal context | `AlignmentResult.intent_id` | active `Intent` at evaluation time |
| Alignment | `relation`, `goal_relevance`, `alignment_confidence`, `reason` | deterministic `GoalAligner` |

**Cache identity.** `Classifier._cache_key` now includes `prompt_version` and
`classifier_version` in addition to the session identity and observed evidence.
A prompt or classifier bump therefore invalidates every cached in-memory verdict.
Goal context is deliberately *not* in the classification cache key because
classification is goal-independent; goal relevance is a separate downstream
computation keyed by `(session_id, intent_id)` in the `alignments` table.

---

## G. Failure semantics (failed ≠ neutral, proven)

| Status | Meaning | Consumed by behavior? | Consumed by alignment? |
|---|---|---|---|
| `pending` | not yet classified / provider unavailable | No | Not as a misalignment (fails the classified gate) |
| `classified` | a real verdict | Yes | Yes |
| `classification_failed` | invalid output / parse error | No (nulled in `_candidate` and `evaluate`) | Never `misaligned` (requires `classification_status == "classified"`) |
| stale (superseded revision) | old prompt/classifier version | re-queued, not served from cache | n/a |

`test_failed_classification_not_consumed_by_alignment_or_behavior` proves a
`classification_failed` row with high confidence and strong evidence still
yields `NORMAL` behavior and a non-`misaligned` alignment. Provider failures
stay failures; no fake neutral is ever synthesized locally.

---

## H. Tests

- **Full suite:** 377 passed, 2 skipped, 0 failed **with the repo-declared
  `google-genai` present**. Without it (bare CI container), the two Gemini SDK
  tests fail at `from google import genai` → 375 passed / 2 skipped / 2 failed.
  Those 2 failures were rigorously verified to be pre-existing and environmental
  (same 2 tests fail identically on the pre-V2 baseline commit `78a9101`; no
  Semantic V2 test imports google-genai).
- **New (Semantic V2 + relevance fixes):** 18 in `tests/regression/test_semantic_v2.py`.
- **Failures:** `test_gemini_thinking_level_flows_to_sdk_and_retries_ambiguous`
  and `test_gemini_keeps_first_verdict_when_medium_retry_fails` fail **only** when
  `google-genai` (a declared dependency) is not installed. Unrelated to this work.

New test coverage maps to the acceptance criteria:

| Test | Criterion |
|---|---|
| `test_goal_related_coding_is_aligned_high_relevance` | relevant productive → aligned/high |
| `test_productive_but_unrelated_is_unknown_not_aligned` | productive ≠ aligned; productive-unmatched → unknown (F2) |
| `test_bonuses_cannot_manufacture_alignment_without_overlap` | bonuses can't fake ALIGNED without overlap (F1) |
| `test_productive_unmatched_work_does_not_drift_end_to_end` | relevant-but-unmatched → unknown → no false drift (F2) |
| `test_distractive_without_evidence_is_unknown_not_misaligned` | distractive ≠ misaligned without evidence |
| `test_insufficient_evidence_is_unknown` | insufficient evidence → unknown |
| `test_clearly_unrelated_with_strong_evidence_is_misaligned` | distractive unrelated + evidence → misaligned |
| `test_no_goal_produces_unknown_alignment_and_no_drift` | no goal → unknown alignment, no drift |
| `test_unknown_alignment_does_not_drift` | unknown ≠ distractive |
| `test_explicit_misaligned_drifts` | misaligned → DRIFTING |
| `test_failed_classification_not_consumed_by_alignment_or_behavior` | failed not consumed |
| `test_legacy_bare_not_aligned_derives_unknown_relation` | backward compat |
| `test_store_round_trips_relation_and_relevance` | persistence |
| `test_goal_change_does_not_rewrite_old_alignment` | goal change preserves history |
| `test_cache_key_changes_with_prompt_version` | cache identity (prompt) |
| `test_cache_key_changes_with_classifier_version` | cache identity (classifier) |
| `test_distinct_sessions_do_not_collide_in_cache` | cache identity (session) |
| `test_raw_fanout_preserves_episode_lineage_not_independent_judgments` | "neutral 40%" regression / lineage |

---

## I. Known limitations (honest)

- **Browser metadata availability.** Alignment quality depends on titles/domains
  actually captured. Where privacy filtering redacts or the source omits them,
  relevance frequently and correctly stays `unknown`.
- **Deterministic (not semantic) relevance.** `GoalAligner` uses token overlap
  plus verb compatibility, not an LLM. This is intentional (inspectable, no model
  can trigger actions), but it misses synonym/paraphrase/acronym relevance
  (e.g. goal "Unitree A1" vs. activity "MuJoCo quadruped"; "yolo" vs "yolo11").
  Per the F1/F2 fixes these land in **`unknown`** (no drift, no false focus), not
  a wrong verdict — a deliberate conservatism, not full coverage. See
  `SEMANTIC_V2_RELEVANCE_AUDIT.md`.
- **DRIFTING is effectively not emitted by deterministic alignment.** Because
  MISALIGNED now requires a *distractive* verdict (which the behavior engine
  already routes to the stronger `DISTRACTED` state), the `DRIFTING` state is not
  reached from `GoalAligner` output in practice. This is intentional: we do not
  deterministically assert "you are drifting on productive work" when we cannot
  reliably tell relevant-but-unmatched from truly-unrelated. `DRIFTING` remains
  in the state machine for a future semantic aligner or an explicit misaligned
  verdict.
- **Residual generic-word overlap.** A single shared generic token (e.g.
  "system") can still cross the alignment bar (goal "recommendation system" vs.
  "System Restore" → aligned). A curated generic-tech stop-list would fix this
  but risks new false negatives, so it was left out of the F1/F2 scope.
- **Goal availability.** With no active `Intent`, all alignment is `unknown`; the
  system does not infer goals from telemetry.
- **Ambiguous multi-goal selection.** The authoritative goal is the most recent
  `Intent`. Richer multi-goal selection is not implemented; ambiguity yields
  `unknown` rather than a guess.
- **User feedback / recomputation.** No new feedback-learning or historical
  recomputation was added; alignment is versioned per `(session, intent)` and old
  rows are not rewritten (legacy `aligned=False` reads back as `unknown`).
- **Causal attribution.** The system reports observable relevance/alignment; it
  does not diagnose the user or infer intent beyond the entered goal.
- **Live validation.** LIVE VALIDATION NOT PERFORMED — this container has no
  desktop, no ActivityWatch, and no Gemini/`google-genai` credentials. All
  validation is via the automated suite against in-memory SQLite and test doubles.

---

## J. Launch claims

**Noema CAN claim today:**

- It distinguishes what the user is doing (semantic category) from whether it is
  relevant to the active goal (goal relevance) from the relationship (alignment).
- Goal relevance and alignment are three-valued and honest: insufficient evidence
  is `unknown`, never a fabricated distraction.
- No active goal → `unknown` alignment, with no drift and no false focus.
- A single weak episode does not become nine independent AI judgments; raw rows
  carry explicit `meaningful_session_id` + `inherited_from` lineage.
- Failed/pending classifications never influence behavior or produce misalignment.
- Interventions remain strictly downstream of behavior, with all existing
  cooldown/hysteresis/AFK protections intact.
- Semantic verdicts are versioned; a prompt/classifier bump invalidates stale
  caches.

**Noema MUST NOT claim:**

- That it knows page/video/message *contents* it did not observe.
- That an app or domain is inherently productive or distracting.
- That `unknown`/`neutral`/`pending`/`failed` means distraction.
- That it performs semantic (LLM) goal-relevance — relevance is deterministic
  token/verb matching and will miss paraphrase relevance (→ `unknown`).
- That it diagnoses the user (laziness, addiction, procrastination).
- That live end-to-end behavior was validated in this environment — it was not.
