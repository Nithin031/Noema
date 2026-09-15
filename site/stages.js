// "How Noema thinks" — eleven pipeline stages, each with an input/process/output
// contract. Content and source paths are taken verbatim from src/noema/.
(function () {
  "use strict";

  var STAGES = [
    {
      id: "OBSERVE", kind: "deterministic",
      input: "Foreground application and window title, process identity, input-presence timing, heartbeat continuity; optionally browser tab metadata through the local bridge.",
      process: "Native Windows collectors poll once a second. A failing source never blocks the others, and an unobservable window closes its span explicitly instead of fabricating a title.",
      output: "Canonical activity events — evidence, never a verdict.",
      source: "infrastructure/activity_sources/native_windows.py · native_presence.py"
    },
    {
      id: "NORMALIZE", kind: "deterministic",
      input: "Raw source events from the native collectors or the read-only ActivityWatch adapter.",
      process: "EventNormalizer canonicalizes the event, then the privacy filter runs fail-closed: URL query strings stripped, private windows redacted, password-manager windows blocked.",
      output: "normalized_events, stored idempotently — restarts never duplicate rows.",
      source: "domain/normalization/normalizer.py · domain/privacy/filter.py"
    },
    {
      id: "SESSIONIZE", kind: "deterministic",
      input: "Per-second normalized events plus presence state.",
      process: "Heartbeats merge into continuous stretches of one context and split on app/domain/title change, AFK gaps and the merge-gap threshold. Recomputation is idempotent.",
      output: "ActivitySession rows — immutable raw evidence.",
      source: "domain/sessions/sessionizer.py"
    },
    {
      id: "UNDERSTAND", kind: "deterministic",
      input: "Consecutive activity sessions and any active intent.",
      process: "Continuity scoring over time, app transition, topic, project and intent context groups related sessions into a task episode. Large gaps lower the score rather than acting as the only boundary.",
      output: "MeaningfulSession with phases, evidence and a deterministic evidence_quality rating: strong / moderate / weak / absent.",
      source: "domain/meaningful/engine.py · continuity.py · confidence.py"
    },
    {
      id: "CLASSIFY", kind: "model",
      input: "A token-budgeted JSON evidence payload: the episode, its raw-session context and preceding sessions.",
      process: "Batch classification on the classification tier, up to 60 episodes per run on a ~20-minute cadence. Thin evidence must resolve to neutral, never distractive.",
      output: "productive / distractive / neutral, confidence, activity type and a cited signal. Provider failures stay pending with retry state.",
      source: "application/classification.py · infrastructure/providers.py"
    },
    {
      id: "ALIGN", kind: "deterministic",
      input: "The classified episode and the user's stated goal.",
      process: "IntentEngine structures the goal locally; GoalAligner scores episode against intent with explainable, persistable results. Productive is not automatically aligned.",
      output: "ALIGNED / MISALIGNED / UNKNOWN — insufficient evidence resolves to UNKNOWN.",
      source: "domain/intent/engine.py · alignment.py"
    },
    {
      id: "DETECT DRIFT", kind: "deterministic",
      input: "Rolling behavior windows over recent session state, independent of the semantic scheduler.",
      process: "A 12-signal local scorer runs every 60 seconds with hysteresis — enter 0.70, exit 0.50, minimum 120s active, 1800s cooldown — so the state does not flap. Fast-model verification runs only on entry and fails safe.",
      output: "A DetectionRecord: candidate, verified or intervention. Never a semantic classification.",
      source: "application/realtime/detector.py · features.py · verifier.py"
    },
    {
      id: "REASON", kind: "model",
      input: "Bounded context: current semantic activity, active goal, alignment, behavioral state, drift score, recent episodes, recent interventions and outcomes, break state.",
      process: "The reasoning tier interprets the situation and may decline outright. A distractive classification does not force an intervention.",
      output: "A structured InterventionDecision — should_intervene, type, severity, tone, response_class, use_meme, confidence — or DO_NOT_INTERVENE.",
      source: "application/realtime/intervention.py · reasoning.py"
    },
    {
      id: "POLICY", kind: "deterministic",
      input: "The model's recommendation plus current device and history state.",
      process: "The gate checks AFK presence, break mode, global cooldown (900s default), per-mode cooldowns (MEME 3600s, NOTIFICATION 900s, HOLDOUT 300s), confidence, evidence quality, delivery availability, and backs off after three consecutive NOT_RECOVERED outcomes.",
      output: "ALLOW or SKIP. A model error can never bypass a product safeguard.",
      source: "domain/intervention/engine.py"
    },
    {
      id: "INTERVENE", kind: "hybrid",
      input: "An allowed decision and its response class.",
      process: "The selector resolves a curated Response deterministically — never free model text. In meme mode, local retrieval shortlists at most 20 candidate assets and the reasoning tier ranks them; contextual copy may only use supplied evidence.",
      output: "Delivery over the browser bridge, with desktop notification fallback. EXECUTED, DISPLAYED, SEEN and ACKNOWLEDGED are distinct records.",
      source: "domain/response/selector.py · application/meme_decision.py"
    },
    {
      id: "MEASURE", kind: "deterministic",
      input: "The typed user action — LOCK_IN, BREAK_5MIN, INTENTIONAL — and subsequent activity.",
      process: "OutcomeTracker watches the recovery window. Recovery requires a later productive episode of at least three minutes; AFK never counts, and classification failures are forced to neutral so they cannot fake a recovery.",
      output: "RECOVERED / NOT_RECOVERED / INDETERMINATE with DIRECT, AMBIENT or NONE attribution.",
      source: "domain/outcomes/tracker.py"
    }
  ];

  var listEl = document.getElementById("stage-list");
  var elNum = document.getElementById("stage-active-num");
  var elId = document.getElementById("stage-active-id");
  var elInput = document.getElementById("stage-active-input");
  var elProcess = document.getElementById("stage-active-process");
  var elOutput = document.getElementById("stage-active-output");
  var elSource = document.getElementById("stage-active-source");

  if (!listEl) return;

  var buttons = [];

  STAGES.forEach(function (stage, i) {
    var li = document.createElement("li");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "stage-btn";
    btn.setAttribute("aria-pressed", i === 0 ? "true" : "false");
    btn.innerHTML =
      '<span class="stage-num mono">' + String(i + 1).padStart(2, "0") + "</span>" +
      '<span class="stage-id mono">' + stage.id + "</span>" +
      '<span class="stage-kind mono">' + stage.kind + "</span>";
    btn.addEventListener("click", function () { select(i); });
    li.appendChild(btn);
    listEl.appendChild(li);
    buttons.push(btn);
  });

  function select(i) {
    var stage = STAGES[i];
    buttons.forEach(function (btn, j) {
      btn.setAttribute("aria-pressed", j === i ? "true" : "false");
    });
    elNum.textContent = String(i + 1).padStart(2, "0");
    elId.textContent = stage.id;
    elInput.textContent = stage.input;
    elProcess.textContent = stage.process;
    elOutput.textContent = stage.output;
    elSource.textContent = "src/noema/" + stage.source;
  }

  select(0);
})();
