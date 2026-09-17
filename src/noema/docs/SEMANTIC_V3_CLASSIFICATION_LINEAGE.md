# Semantic V3 — Classification Lineage Audit

Forensic trace of Noema's semantic classification pipeline, the root causes of
the "0.78 fan-out" failure, and the architecture that makes it structurally
impossible to reproduce.

---

## 1. The observed failure

```
21:16  Firefox / "Noema"     -> episode 79bc95ca created
21:16  classifier runs       -> PRODUCTIVE 0.78 stored for 79bc95ca
21:16..21:36                    episode keeps growing (same id)
        Brave, VS Code, OpenCode, Explorer, Cinépolis, unrelated browsing
21:36  dashboard             -> every one of those rows renders PRODUCTIVE 0.78
```

One model call over roughly four minutes of Firefox evidence was presented as
an independent, current judgment about twenty minutes and six applications.

## 2. Lineage map (pre-V3)

```
ActivityEvent            store.insert_events
  -> ActivitySession     domain/sessions/sessionizer.py
  -> MeaningfulSession   domain/meaningful/engine.py          (episode)
       id survives growth  infrastructure/database/store.py:_insert_one_meaningful_locked
  -> Classification      application/classification.py        (one row per episode id)
       persisted          semantic_classifications (session_id PRIMARY KEY)
  -> eligibility         application/pipeline.py:_is_eligible
  -> presentation        pipeline.recent_activity / daily_summary / current_activity
  -> frontend            web/src/views/results/index.tsx
```

For every verdict the brief asks twelve questions. Pre-V3, the pipeline could
answer four of them:

| # | Question | Pre-V3 answer |
|---|----------|---------------|
| 1 | What semantic unit was classified? | MeaningfulSession id — **but the unit mutates under the id** |
| 2 | What evidence existed at classification time? | **Not recorded** |
| 3 | What interval did the evidence cover? | **Not recorded** |
| 4 | What model produced it? | `model` column ✅ |
| 5 | What prompt version? | `prompt_version` column ✅ |
| 6 | What classifier version? | `classifier_version` column ✅ |
| 7 | What provider? | `provider` column ✅ |
| 8 | Direct or inherited? | Computed at render time, discarded afterwards |
| 9 | What descendants inherited it? | **Unbounded and unrecorded** |
| 10 | What newer evidence appeared? | **Never compared** |
| 11 | Is the verdict still valid? | **Unanswerable — no window to compare against** |
| 12 | What triggers reclassification? | Only a classifier-version bump |

## 3. Root causes

### RC-1 — Episode identity outlives evidence identity
`store.py:_insert_one_meaningful_locked` deliberately keeps the *existing*
episode id when a rebuilt episode shares any member session and overlaps in
time. That is correct for episode continuity. But `semantic_classifications`
is keyed `session_id PRIMARY KEY` on that same id, so the **verdict silently
inherits episode identity**. A verdict formed from four minutes of evidence
keeps describing the episode after it grows to twenty.

### RC-2 — Eligibility is evidence-blind
`pipeline.py:_is_eligible` returns `False` for any row that is `classified`
under the current classifier version. There is no comparison between what the
verdict saw and what the episode now contains. Once classified, an episode is
**never reconsidered** no matter how much contradictory evidence arrives. The
scheduler asks `pending_classification_sessions`, so the grown episode is not
even in the queue.

### RC-3 — No evidence window is persisted
Neither `Classification` nor `semantic_classifications` carried
`evidence_start`, `evidence_end`, an evidence digest, provenance, freshness or
a revision number. Even a component that *wanted* to ask "is this still valid?"
had no data to answer with. The verdict was structurally an eternal property of
a row.

### RC-4 — Unbounded inheritance fan-out
`recent_activity`, `daily_summary` and `current_activity` each build a
`parent_of` map and copy the episode verdict onto every member raw row **with
no check that the verdict's evidence covers that row's time**. `inherited_from`
was set, but the rendered values (`PRODUCTIVE`, `78%`) were identical to a
direct verdict, so the UI showed twenty minutes of independent-looking
judgments produced by one call.

### RC-5 — No way to express uncertainty
`_ALLOWED_CATEGORIES = {productive, distractive, neutral}`. `neutral` served
both "understood, uncategorizable" and "evidence insufficient", which the brief
separates (`unknown != neutral`). There was no state for "I had a verdict and
new evidence contradicts it".

### RC-6 — Cache identity is not semantic identity
`Classifier._cache_key` hashed session id, app/title/domain/url,
project/topic, the passed context and the two versions. For a MeaningfulSession
every one of app/title/domain/url is `None`. Episode **end time, active
duration and member set were absent**, as were provider and model. Correctness
depended on the caller happening to pass a full member list.

### RC-7 — Behavior and interventions are freshness-blind
`BehaviorEngine` correctly ignores pending/failed rows but has no notion of
staleness: an inherited hour-old verdict drove `DISTRACTED -> actionable ->
intervention` exactly like a fresh one. `consider_intervention` read the
classification with no freshness or provenance check.

**The failure is the composition of RC-1, RC-2 and RC-4.** Fixing any one alone
leaves the other two able to reproduce a variant of it.

---

## 4. V3 architecture

### 4.1 The verdict contract

```
CLASSIFICATION = VERDICT
               + EVIDENCE WINDOW
               + PROVENANCE
               + MODEL/PROMPT/CLASSIFIER VERSION
               + TEMPORAL SCOPE
```

`EvidenceWindow` (`domain/semantic/evidence_window.py`) is a value object:
`start`, `end`, `observation_count`, `active_seconds`, and a `digest` over the
exact observations shown to the model.

A verdict with no recorded window (only pre-V3 rows) **covers nothing**:
`Classification.covers()` returns `False` and its freshness reads `STALE`.
Unknown scope is never treated as total scope. Every verdict produced by the
V3 path is stamped with its window before persistence.

### 4.2 Distinct concepts, never collapsed

| Concept | Home |
|---|---|
| A. Raw activity | `domain/activity.py` |
| B. Activity session | `domain/sessions/` |
| C. Semantic episode | `domain/meaningful/` |
| D. Evidence window | `domain/semantic/evidence_window.py` |
| E. Classification verdict | `application/classification.py` |
| F. Provenance | `domain/semantic/provenance.py` |
| G. Freshness | `domain/semantic/provenance.py` |
| H. Confidence | `Classification.confidence` |
| I. Evidence quality | `domain/episodes/models.py`, `meaningful/confidence.py` |
| J. Intent alignment | `domain/intent/alignment.py` (unchanged) |
| K. Behavioral drift | `domain/behavior/` |

### 4.3 The structural fix: time-bounded resolution

Presentation no longer asks "which episode owns this row?" and copies its
verdict. It asks:

> **Which verdict's evidence window contains this row?**

`resolve_for_interval` returns a verdict only when the verdict's evidence
window actually covers the row. A row outside every window resolves to
`UNCERTAIN`/`pending` — never to the episode's first verdict.

This is what makes the original failure impossible *in form*, not just in
degree: the 21:36 rows are not inside the 21:16–21:20 window, so there is no
code path that can stamp 0.78 on them.

### 4.4 Verified window extension

A verdict may come to cover evidence formed after it, but only on a
positive finding. Stage A checks each new observation for application
transitions, domain transitions, topical novelty, entropy shift and
contradiction. When it finds none, the verdict's window extends over that
evidence with provenance `REUSED` — no model call, verdict and confidence
untouched, and a recorded reason saying the model was not re-asked.

The distinction that makes this safe, rather than a smaller version of the
original defect:

| Stage A outcome | May the window extend? |
|---|---|
| Examined the new evidence, found continuity | **Yes** — `examined=True` |
| Declined to look (below the 90s new-evidence floor) | **No** |
| Found a material change | **No** — reconsider instead |

"I checked and it is consistent" and "there was too little to bother
checking" are different conclusions. Only the first licenses coverage. This
is enforced by `ChangeAssessment.examined` and pinned by
`test_unexamined_evidence_below_the_floor_is_never_covered` — the case where
a 20-second entertainment blip would otherwise be absorbed into a work
verdict.

Wall-clock aging still applies, so a long-unexamined verdict stops reading
as current even while its window covers the span.

### 4.5 Two-stage revision

```
episode grows
  -> Stage A  EvidenceChangeDetector          (local, free)
       signals: app/domain transition, semantic novelty, evidence composition
                shift, entropy change, verdict contradiction, low confidence,
                low evidence quality, sufficient new active duration
     -> RETAIN            no model call
     -> RECONSIDER        continue to Stage B
  -> Stage B  model call over a bounded, structured evidence window
     -> RETAIN | REVISE | UNCERTAIN | SEGMENT
```

A stable episode gets progressively cheaper: each tick costs one local
comparison, not a Gemini call.

### 4.6 Semantic segments, not episode destruction

An episode keeps its identity and temporal continuity. Semantic change creates
a **segment** — a bounded sub-interval with its own verdict, its own evidence
window and its own provenance:

```
EPISODE 79bc95ca  21:16-21:36
  SEG A 21:16-21:20  PRODUCTIVE 0.78  DIRECT
  SEG B 21:20-21:27  UNCERTAIN        RECLASSIFIED (mixed evidence)
  SEG C 21:27-21:36  DISTRACTIVE 0.91 RECLASSIFIED
```

### 4.7 Semantic state machine

```
UNKNOWN -> EMERGING -> STABLE -> CHANGING -> RECLASSIFYING -> STABLE
```

`UNCERTAIN` is a terminal-safe state: it never becomes an intervention, never
silently becomes PRODUCTIVE, and never inherits a parent verdict.

---

### 4.8 Boundary semantics

Coverage tolerance absorbs sub-second clock skew between the sessionizer
and the episode builder. It must never let a verdict reach *content* it
never observed, so `covers_interval` adds two conditions:

* a zero-length interval is tested half-open, `[start, end)` — the instant
  an episode ends belongs to whatever comes next;
* a real interval must have the majority of its duration inside the window.

Found by adversarial replay: a Netflix row starting exactly where a work
verdict's evidence stopped was inheriting "productive" through the
tolerance.

## 5. Measured behavior

Driven through `NoemaService` with a scripted model (see §23 of the report):

| Scenario | Model calls | Result |
|---|---|---|
| Stable episode, 9 scheduler ticks over 3h | **1** | 9/9 rows covered, window extended 10:00→13:00 `[reused]` |
| Episode alternating work/entertainment, 6 ticks | **3** | 2 segments, each bound to its own window |
| Forensic replay (21:16 → 21:36) | **2** | `productive 0.78` on 21:16–21:20 only; rest revised |

## 6. Invariants enforced by tests

```
NO FAILED CLASSIFICATION MAY BECOME A VERDICT
NO UNCERTAIN CLASSIFICATION MAY BECOME AN INTERVENTION
NO STALE VERDICT MAY BE PRESENTED AS CURRENT
NO INHERITED VERDICT MAY BE PRESENTED AS DIRECT
NO CLASSIFICATION MAY EXIST WITHOUT AN EVIDENCE WINDOW
NO CACHE HIT MAY BYPASS SEMANTIC IDENTITY
NO MODEL OUTPUT MAY BYPASS VALIDATION
```

See `tests/regression/test_semantic_v3.py` (59 tests).

---

## 7. Deliberately unchanged

- Episode construction (`MeaningfulSessionEngine`) — continuity scoring is
  sound; V3 adds revision on top rather than replacing it.
- Episode id stability on growth — correct for continuity; the bug was the
  verdict inheriting that stability, which V3 severs.
- Three-valued alignment (`aligned/misaligned/unknown`) — already correct and
  goal-independent from classification.
- Provider chain, tiered routing, quota ledger, intervention hysteresis,
  curated Meme Center, privacy boundaries, local REST API.
