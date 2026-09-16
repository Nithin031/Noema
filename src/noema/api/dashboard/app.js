// Wires this page to the real local Noema daemon. Same origin as the API
// (this file is only ever served by NoemaApp), so every call below is a
// plain relative fetch — no cross-origin reachability guessing needed.
//
// Every element that shows product data starts with the illustrative
// example already in the HTML (see index.html), tagged EXAMPLE. On the
// first successful fetch with real rows, this script replaces that content
// and flips the tag to LIVE. If a call fails (daemon not attached, empty
// database, source unavailable) the illustrative content is left in place
// — nothing here fabricates data to look more finished than the backend
// actually is.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    return String(value == null ? "—" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  };

  function get(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) return r.json().then(function (body) { throw new Error(body.error || (r.status + " " + path)); }, function () { throw new Error(r.status + " " + path); });
      return r.json();
    });
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || String(r.status));
        return data;
      });
    });
  }

  function duration(seconds) {
    var n = Number(seconds || 0);
    if (n < 60) return Math.round(n) + "s";
    var h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60);
    return h > 0 ? (h + "h " + m + "m") : (m + "m " + Math.round(n % 60) + "s");
  }

  function setTag(el, live) {
    if (!el) return;
    el.className = "data-tag mono " + (live ? "live" : "example");
    el.innerHTML = '<span class="dot"></span>' + (live ? "LIVE" : "EXAMPLE");
  }

  function timeRange(startIso, endIso) {
    function hm(iso) {
      var d = new Date(iso);
      return isNaN(d) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return hm(startIso) + " → " + hm(endIso);
  }

  /* ── Card 1: latest semantic episode ── */
  function renderEpisode(session) {
    if (!session) { setTag($("episode-tag"), false); return; }
    setTag($("episode-tag"), true);
    $("episode-time").textContent = timeRange(session.start_time, session.end_time);
    $("episode-status").textContent = (session.status || "unknown").toUpperCase();
    $("episode-topic").textContent = session.primary_topic || session.primary_task || session.primary_project || "Untitled episode";
    $("episode-evidence").textContent = session.evidence_quality || "—";
    $("episode-focus").textContent = session.focus_score != null ? session.focus_score.toFixed(2) : "—";
    $("episode-switches").textContent = session.context_switch_count != null ? session.context_switch_count : "—";
    $("episode-outcome").textContent = session.outcome || "unknown";
  }

  /* ── Card 2: behavior state + active intent ── */
  function renderBehavior(observation, intent) {
    var hasData = !!observation || !!intent;
    setTag($("behavior-tag"), hasData);
    if (intent && intent.text) {
      $("active-intent").textContent = "“" + intent.text + "”";
    }
    if (observation) {
      var distraction = observation.distraction_score;
      $("behavior-distraction").textContent = distraction != null ? distraction.toFixed(2) : "—";
      var tags = [];
      var state = String(observation.state || "").toUpperCase();
      if (state) tags.push({ text: state, cls: state === "DISTRACTED" ? "tag-accent" : "tag-outline" });
      if (observation.actionable) tags.push({ text: "ACTIONABLE", cls: "tag-neutral" });
      if (tags.length) {
        $("behavior-tags").innerHTML = tags.map(function (t) {
          return '<span class="tag ' + t.cls + ' mono" style="font-size:10px;letter-spacing:.1em;">' + esc(t.text) + "</span>";
        }).join("");
      }
    }
  }

  /* ── Card 3: latest reasoning decision ── */
  function renderDecision(decision) {
    if (!decision) { setTag($("decision-tag"), false); return; }
    setTag($("decision-tag"), true);
    $("decision-json").textContent = JSON.stringify(decision, null, 2);
  }

  /* ── Card 4: latest delivered intervention + typed actions ── */
  var currentInterventionId = null;
  function renderIntervention(item) {
    var open = item && (item.status === "PLANNED" || item.status === "EXECUTED");
    currentInterventionId = open ? item.id : null;
    ["btn-lock-in", "btn-break", "btn-intentional"].forEach(function (id) {
      $(id).disabled = !open;
    });
    if (!item) { setTag($("intervention-tag"), false); return; }
    setTag($("intervention-tag"), true);
    var msg = item.message || {};
    $("intervention-mode").textContent = "OVERLAY · " + (item.mode ? item.mode.toUpperCase() : "MODE") + (open ? "" : " · CLOSED");
    $("intervention-title").textContent = msg.title || item.reason || "Noema intervention";
    $("intervention-body").textContent = msg.body || (item.outcome ? "Outcome: " + item.outcome.recovery_status : "");
  }

  function setActionStatus(el, message, ok) {
    el.textContent = message;
    el.className = "mono action-status " + (ok === true ? "ok" : ok === false ? "err" : "");
  }

  /* ── Outcomes / response effectiveness ── */
  function renderEffectiveness(rows) {
    if (!rows || !rows.length) { setTag($("outcomes-tag"), false); return; }
    setTag($("outcomes-tag"), true);
    var shows = 0, recoveries = 0;
    rows.forEach(function (row) {
      shows += Number(row.times_shown || 0);
      recoveries += Number(row.recovery_count || 0);
    });
    var rate = shows > 0 ? Math.round((recoveries / shows) * 100) : null;
    var verdict = shows < 5
      ? "“Not enough data”"
      : (rate + "% recovery rate");
    $("outcomes-summary").innerHTML =
      '<span>' + shows + ' shown · ' + recoveries + ' recovered</span>' +
      '<span style="color: var(--color-neutral-600);">→</span>' +
      '<span style="color: var(--color-accent-200);">' + esc(verdict) + '</span>';
  }

  /* ── Meme Center stats ── */
  // store.meme_asset_stats() reports live corpus totals (assets, favorites,
  // by_sentiment, curated_assets, active_responses) but not the one-time
  // ingestion report's invalid-row count, so that figure stays the
  // documented example until a live ingest endpoint exposes it.
  function renderMemeStats(stats) {
    if (!stats || !stats.assets) { setTag($("meme-tag"), false); return; }
    setTag($("meme-tag"), true);
    $("meme-total").textContent = Number(stats.assets).toLocaleString();
    $("meme-valid").textContent = Number(stats.assets).toLocaleString();
  }

  /* ── Live activity table ── */
  function renderActivity(events) {
    var body = $("activity-rows");
    if (!events || !events.length) {
      body.innerHTML = '<tr><td colspan="5" style="padding: 10px 16px; color: var(--color-neutral-500);">No events yet. Start the collector and daemon.</td></tr>';
      return;
    }
    body.innerHTML = events.map(function (e) {
      var time = (e.timestamp || e.time || "").replace("T", " ").slice(0, 19);
      var signal = esc(e.productivity || "Unclassified") +
        (e.evidence_quality ? " · " + esc(e.evidence_quality) : "") +
        (e.classification_confidence != null ? " · " + Math.round(Number(e.classification_confidence) * 100) + "%" : "");
      return "<tr>" +
        "<td style=\"padding:10px 16px;border-bottom:1px solid var(--color-divider);color:var(--color-neutral-400);\">" + esc(time) + "</td>" +
        "<td style=\"padding:10px 16px;border-bottom:1px solid var(--color-divider);color:var(--color-neutral-200);\">" + esc(e.app || e.application) + "</td>" +
        "<td style=\"padding:10px 16px;border-bottom:1px solid var(--color-divider);color:var(--color-neutral-500);\">" + esc(e.domain || e.url || "") + "</td>" +
        "<td style=\"padding:10px 16px;border-bottom:1px solid var(--color-divider);color:var(--color-neutral-400);\">" + duration(e.duration) + "</td>" +
        "<td style=\"padding:10px 16px;border-bottom:1px solid var(--color-divider);color:var(--color-neutral-400);\">" + signal + "</td>" +
        "</tr>";
    }).join("");
    $("activity-updated").textContent = "Updated " + new Date().toLocaleTimeString();
  }

  /* ── Daemon health (hero line + developers panel) ── */
  function renderHealth(health) {
    var line = $("daemon-status-line");
    var panel = { status: $("health-status"), uptime: $("health-uptime"), provider: $("health-provider"), workers: $("health-workers") };
    if (!health) {
      line.textContent = "○ Daemon not attached — the API is serving without the background daemon.";
      line.className = "mono app-status offline";
      panel.status.textContent = "not attached";
      panel.uptime.textContent = "—";
      panel.provider.textContent = "—";
      panel.workers.textContent = "—";
      return;
    }
    line.textContent = "● LOCAL CORE ONLINE · " + (health.status || "running") + (health.pid ? " · pid " + health.pid : "");
    line.className = "mono app-status online";
    panel.status.textContent = health.status || "—";
    panel.uptime.textContent = health.uptime_seconds != null ? duration(health.uptime_seconds) : "—";
    panel.provider.textContent = health.provider ? (health.provider + (health.provider_model ? " · " + health.provider_model : "")) : "—";
    var workers = health.workers || {};
    var healthy = Object.keys(workers).filter(function (k) { return workers[k] && workers[k].status === "ok"; }).length;
    panel.workers.textContent = Object.keys(workers).length ? (healthy + " / " + Object.keys(workers).length + " healthy") : "—";
  }

  /* ── Model routing: live classifier status ── */
  function renderClassifierStatus(status) {
    if (!status) {
      $("classifier-state").textContent = "unavailable";
      $("classifier-pending").textContent = "—";
      $("classifier-done").textContent = "—";
      $("classifier-model").textContent = "—";
      return;
    }
    $("classifier-state").textContent = status.status || "—";
    $("classifier-pending").textContent = status.pendingCount != null ? status.pendingCount : "—";
    $("classifier-done").textContent = status.classifiedCount != null ? status.classifiedCount : "—";
    $("classifier-model").textContent = status.model || "—";
  }

  /* ── Main polling refresh ── */
  function refresh() {
    get("/api/daemon/health").then(renderHealth, function () { renderHealth(null); });
    get("/api/classification/status").then(renderClassifierStatus, function () { renderClassifierStatus(null); });

    // The store orders meaningful sessions oldest-first, so take the last
    // page's last row rather than limit=1 (which would be the oldest ever).
    get("/api/meaningful-sessions?limit=200").then(function (d) {
      var sessions = d.sessions || [];
      renderEpisode(sessions[sessions.length - 1]);
    }, function () { renderEpisode(null); });

    Promise.all([
      get("/api/behavior?limit=1").catch(function () { return { observations: [] }; }),
      get("/api/intents?limit=1").catch(function () { return { intents: [] }; }),
    ]).then(function (results) {
      renderBehavior(results[0].observations[0], results[1].intents[0]);
    });

    get("/api/interventions/feed?limit=1").then(function (d) {
      var item = (d.interventions || [])[0];
      renderDecision(item ? item.intervention_decision : null);
      renderIntervention(item);
    }, function () { renderDecision(null); renderIntervention(null); });

    get("/api/responses/effectiveness").then(function (d) { renderEffectiveness(d.effectiveness); }, function () { renderEffectiveness(null); });

    get("/api/meme-assets/stats").then(renderMemeStats, function () { renderMemeStats(null); });

    get("/api/events?limit=15&sort=desc").then(function (d) { renderActivity(d.events); }, function () {
      $("activity-rows").innerHTML = '<tr><td colspan="5" style="padding: 10px 16px; color: var(--color-neutral-500);">Activity source unavailable.</td></tr>';
    });
  }

  /* ── Wire up controls ── */
  $("intent-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var input = $("intent-input");
    var text = input.value.trim();
    if (!text) return;
    var status = $("intent-status");
    post("/api/intents", { text: text }).then(function () {
      setActionStatus(status, "Intent saved locally.", true);
      input.value = "";
      refresh();
    }, function (e) { setActionStatus(status, e.message, false); });
  });

  $("btn-run-classification").addEventListener("click", function () {
    var btn = this, status = $("classifier-action-status");
    btn.disabled = true;
    setActionStatus(status, "Running classification…", null);
    post("/api/ai/run").then(function (result) {
      var classified = result.classifications || result.meaningful_classifications || 0;
      setActionStatus(status, "Done — " + classified + " sessions classified.", true);
      refresh();
    }, function (e) { setActionStatus(status, e.message, false); }).finally(function () { btn.disabled = false; });
  });

  $("btn-stop-ollama").addEventListener("click", function () {
    var btn = this, status = $("classifier-action-status");
    btn.disabled = true;
    post("/api/ollama/stop").then(function (result) {
      setActionStatus(status, result.stopped ? "Model unloaded." : (result.message || "No model was running."), !!result.stopped);
    }, function (e) { setActionStatus(status, e.message, false); }).finally(function () { btn.disabled = false; });
  });

  function interventionAction(action, extra) {
    if (!currentInterventionId) return;
    var status = $("intervention-action-status");
    var path = "/api/interventions/" + encodeURIComponent(currentInterventionId) + "/" + (extra ? extra.endpoint : "action");
    var body = extra ? extra.body : { action: action };
    post(path, body).then(function () {
      setActionStatus(status, action + " recorded.", true);
      refresh();
    }, function (e) { setActionStatus(status, e.message, false); });
  }

  $("btn-lock-in").addEventListener("click", function () { interventionAction("LOCK_IN"); });
  $("btn-break").addEventListener("click", function () {
    interventionAction("BREAK_5MIN", { endpoint: "break", body: { minutes: 5 } });
  });
  $("btn-intentional").addEventListener("click", function () {
    interventionAction("INTENTIONAL", { endpoint: "feedback", body: { feedback_type: "INTERPRETATION", value: "WRONG" } });
  });

  refresh();
  setInterval(refresh, 30000);
})();
