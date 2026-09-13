# Semantic V2 Follow-up — Goal-Relevance Quality Audit

> **Status: RESOLVED.** This document records the audit findings; the two
> dangerous bugs it identified (C1 false-ALIGNED, C2 false-MISALIGNED) were
> subsequently fixed deterministically (F1 + F2 below) with regression coverage.
> See `SEMANTIC_V2_STATUS.md` §E/§I for the shipped behavior. The audit text is
> kept as the "before" record and the rationale for the fixes.
>
> Findings below describe the code state **at audit time** (commit `eeebe73`).
> All results came from running the **actual** `GoalAligner.align(...)` against
> realistic goal/activity pairs.

---

## A. Current algorithm

`GoalAligner.align(session, intent, classification)` — fully deterministic:

1. **Tokenize** both sides on `[a-z0-9]+`, lowercase, then subtract a stop-word set.
   - Intent tokens: `text + goal + topic + project + success_criteria + keywords`.
   - Session tokens: `app + domain + url + title` **plus** the classification's
     `category + topic + project + activity_type`. (Note: the classification's
     free-text `activity`/`signal` are **not** tokenized.)
2. **overlap** = intent_tokens ∩ session_tokens; `overlap_score = |overlap| / |intent_tokens|`.
3. **score** = `min(1, overlap_score·0.60 + productivity_bonus + compatibility_bonus)`
   - `productivity_bonus` = 0.15 if classification productivity == "productive".
   - `compatibility_bonus` = 0.25 if a goal verb (implement/code/build/debug/read/
     research/write/study) maps to the classification's `activity_type`/`category`
     (`VERB_COMPATIBILITY`).
4. **Decision** (threshold = 0.35):
   - `score ≥ 0.35` → **aligned** (relevance high if `score ≥ 0.6` else medium).
   - `score < 0.35` and some overlap → **unknown** (relevance low).
   - no overlap **and** a confident, well-evidenced *classified* verdict
     (`confidence ≥ 0.5`, `evidence_quality ∈ {strong, moderate}`) → **misaligned**.
   - otherwise → **unknown**.

Matching is **purely lexical**: no stemming, no lemmatization, no synonyms, no
acronym expansion, no substring/fuzzy matching, no multi-word phrase logic.

---

## B. Strengths

- **Right architecture.** Relevance is deterministic and inspectable; no model
  decides "you're wasting time." An LLM says *what* is happening; deterministic
  logic says *whether it matches the goal*; the temporal engine says *whether it
  persists*; policy says *whether to interrupt*. This audit does **not** challenge
  that separation — it is the correct, defensible design.
- **Stop-word filtering works.** Generic connectives ("for","the","of") no longer
  fake overlap (a bug fixed in V2).
- **UNKNOWN is a real state (V2).** Thin evidence and no-goal cases correctly
  resolve to `unknown` and no longer drift. Verified: `Google / 4s` → unknown;
  no observed activity → unknown.
- **Distractive unrelated activity is caught.** Entertainment/shopping with strong
  evidence → misaligned (cat. YOLO-vs-cats, exam-vs-laptop-shopping) ✓.
- **Conservative on missing evidence.** Empty activity → unknown (not a guess).

---

## C. Weaknesses (two structural, both confirmed with evidence)

### C1. Bonuses manufacture ALIGNED with **zero** semantic overlap — false positives that HIDE drift
`productivity_bonus (0.15) + compatibility_bonus (0.25) = 0.40`, which already
exceeds the `0.35` threshold. So **"productive work whose type matches any verb in
the goal"** is declared ALIGNED regardless of topic. This is the single most
dangerous failure per your own criterion ("a false ALIGNED can hide drift").

### C2. Lexical-only matching misses paraphrase/synonym/acronym/morphology — false negatives that CREATE drift
With no stemming or world knowledge, genuinely relevant but differently-worded
work has zero overlap → (after the bonus doesn't save it) → **misaligned** →
false DRIFTING. Confirmed: `yolo` ≠ `yolo11`; `detect` ≠ `detection`; `docs` ≠
`documentation`; `Unitree A1` unrelated to `quadruped`/`MuJoCo`.

> Note on lineage to V2: C1 and C2 are **pre-existing** (the score formula and
> lexical matching predate V2 and were kept unchanged). V2 did not worsen them;
> it improved surrounding behavior by adding UNKNOWN. But the misaligned→DRIFTING
> path V2 introduced means C2's false negatives now surface specifically as
> **false drift**, so C2 matters more now.

### Per-dimension findings
| # | Dimension | Status |
|---|---|---|
| 1 | Tokenization | OK (`[a-z0-9]+`, lowercased) |
| 2 | Stop-word removal | OK |
| 3 | Stemming / lemmatization | **MISSING** — detect≠detection, docs≠documentation |
| 4 | Synonym handling | **MISSING** — RL matched only by luck (bonus), not by meaning |
| 5 | Acronym handling | **MISSING** — yolo≠yolo11; RL≠"reinforcement learning" |
| 6 | Technical terminology | **MISSING** — quadruped/MuJoCo unrelated to Unitree/A1 |
| 7 | Multi-word concepts | Partial — matches on any shared word ("power electronics"~"electronics" OK; but also generic "system" false-matches) |
| 8 | Verb/action matching | Present but **over-weighted** (see C1) |
| 9 | Project names | OK when present verbatim; brittle to variants |
| 10 | Proper nouns | Literal only |
| 11 | Abbreviations | **MISSING** |
| 12 | Very short goals | Fragile — 1-word "YOLO" vs "YOLO11" → misaligned |
| 13 | Very long goals | Larger denominator lowers overlap_score → biases toward unknown/misaligned |
| 14 | Multi-concept goals | Any one concept matching is enough (can over-align) |
| 15 | Different vocabulary | **Core weakness** (C2) |
| 16 | False overlap from generic words | Present — "system" alone crossed threshold |
| 17 | False negative semantic matches | Present (C2) |
| 18 | Empty/missing goal | Pipeline passes no alignment → unknown ✓ (Intent cannot be empty) |
| 19 | Empty/missing activity | → unknown ✓ |
| 20 | Confidence thresholds | Single fixed 0.35; bonuses can reach it alone (C1) |

---

## D. Concrete FALSE POSITIVES (TRUE UNRELATED → ALIGNED) — most dangerous

| Goal | Activity | Result | Why |
|---|---|---|---|
| Study Power Electronics | Read about reinforcement learning | **aligned** (0.400) | verb "study"→reading + productive; overlap ∅ |
| Study Power Electronics | Reading Roman-empire history | **aligned** (0.400) | same bonus path; overlap ∅ |
| Implement the parser | Reading cooking recipes | **aligned** (0.400) | verb "implement"→coding + productive; overlap ∅ |
| Build the recommendation system | "System Restore" utility | **aligned** (0.350) | generic token "system" |

All four would read **FOCUSED** and silently hide the fact that the user is off-goal.

---

## E. Concrete FALSE NEGATIVES (TRUE RELEVANT → MISALIGNED/UNKNOWN) — create false drift

| Goal | Activity | Result | Why |
|---|---|---|---|
| Implement YOLO crater detection | YOLO11 segmentation documentation | **misaligned** | `yolo` ≠ `yolo11`, no substring/stem |
| Build a Unitree A1 controller | MuJoCo quadruped simulation | **misaligned** | needs world knowledge (A1 = quadruped) |
| "YOLO" (short goal) | YOLO11 segmentation | **misaligned** | version-suffix mismatch |

The two robotics cases are exactly your example class ("obviously relevant, weak
token overlap") and they currently produce **false DRIFTING**, not a benign miss.

Also note the two "RELEVANT→ALIGNED" cases in the primary harness (A1-standing-vs-PPO,
interview-vs-VLA) were **correct only by coincidence** — the bonus crossed the
threshold; overlap was empty. The same mechanism produces the D false positives.

---

## F. Minimum improvement required

Two conservative, deterministic fixes (no LLM), in priority order:

1. **Stop bonuses from manufacturing ALIGNED (fixes C1 / all of D).**
   Require genuine semantic overlap before ALIGNED. Concretely: the
   productivity/compatibility bonuses should only *refine* a score that already
   has non-empty meaningful overlap — they must not, alone, cross the alignment
   threshold. With no overlap, the ceiling should stay below 0.35. This removes
   every false ALIGNED in D and makes those cases UNKNOWN (safe).

2. **Make MISALIGNED require positive evidence of unrelatedness (fixes C2 / all of E).**
   "No lexical overlap" is a weak basis for MISALIGNED because matching is lexical
   only. Restrict MISALIGNED to cases with *positive* contradiction — chiefly
   `category == "distractive"` (entertainment/gaming/shopping) with strong
   evidence — and let "confident but non-matching productive work" fall to
   **UNKNOWN** instead of MISALIGNED. Post-V2, UNKNOWN does not drift, so the
   YOLO11/MuJoCo cases stop producing false drift while the genuinely distractive
   cases (cat-video, laptop-shopping) still correctly misalign.

Optional, lower priority (only if done carefully, risk of new false overlap):
light suffix normalization (detection→detect, documentation→document) and a small
curated acronym map. These reduce E further but are not required for safety.

Guiding rule (yours): *if deterministic evidence is insufficient → UNKNOWN.* Both
fixes push ambiguity toward UNKNOWN rather than a confident wrong verdict.

---

## G. Recommendation: **IMPROVE** (keep the architecture; do not replace with an LLM)

- **Keep** deterministic relevance and the four-stage separation. Correct and safe.
- **Do NOT** add an LLM alignment call. It would let a model decide drift → popup.
- **Improve** with the two conservative fixes in F. Both are "clear relevance bugs"
  (a dangerous false-ALIGNED path and a false-drift MISALIGNED path), which matches
  your step 3 ("fix only clear relevance bugs"). Estimated blast radius is small
  and fully unit-testable; the existing V2 tests already pin the safe directions.
- After the fixes: re-run this harness + full suite, then freeze the core and move
  to real desktop validation + product UX — no new model/provider/subsystem first.

**Net:** V2's separation of relevance/alignment/uncertainty is sound and worth
keeping. The relevance *scorer* it inherited has two concrete, dangerous bugs
(false ALIGNED from bonuses; false MISALIGNED from lexical-only matching). Both
are fixable deterministically and conservatively, without an LLM.
