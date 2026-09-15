const API = "";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
async function get(path){const r=await fetch(API+path,{cache:"no-store"});if(!r.ok)throw new Error(`${r.status} ${path}`);return r.json()}
async function post(path,body={}){const r=await fetch(API+path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw new Error(data.error||`${r.status}`);return data}
function rows(items){return items.map(([a,b])=>`<div class="row"><span>${esc(a)}</span><span>${esc(b)}</span></div>`).join("")}

function duration(v) {
  const n = Number(v || 0);
  if (n < 60) return `${Math.round(n)}s`;
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.round(n % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}

function durationShort(v) {
  const n = Number(v || 0);
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.round(n % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function renderPulse(list) {
  const values = list.slice(0, 30).map(e => Math.max(4, Math.min(100, Number(e.duration || 0) * 2 + 8))).reverse();
  $("pulse").innerHTML = values.length
    ? values.map((v, i) => `<i class="pulse-bar" style="height:${v}%;animation-delay:${i * 18}ms" title="${esc(duration(list[list.length - 1 - i]?.duration))}"></i>`).join("")
    : `<div class="empty-chart">Your activity waveform will appear here.</div>`;
}

/* ═══ Collector Summary Panel ═══ */
function renderSourceSummary(aw) {
  if (!aw || !aw.available) {
    $("aw-active-time").textContent = "—";
    $("aw-meta").textContent = aw?.reason || "Collector unavailable";
    $("aw-top-apps").innerHTML = `<div class="muted">${esc(aw?.reason || "No data")}</div>`;
    $("aw-top-titles").innerHTML = `<div class="muted">${esc(aw?.reason || "No data")}</div>`;
    return;
  }

  // Large active time
  $("aw-active-time").textContent = duration(aw.active_seconds);
  const dateStr = aw.date || new Date().toISOString().slice(0, 10);
  $("aw-meta").innerHTML = `<span class="aw-date">${esc(dateStr)}</span> · Last event: ${esc(aw.latest_event_at ? new Date(aw.latest_event_at).toLocaleTimeString() : "—")}`;

  // Top applications — bar chart
  const apps = aw.top_applications || [];
  const maxAppSec = apps.length ? apps[0].seconds : 1;
  if (apps.length) {
    $("aw-top-apps").innerHTML = apps.slice(0, 8).map(app => {
      const pct = Math.max(2, (app.seconds / maxAppSec) * 100);
      const name = app.name.replace(/\.exe$/i, "");
      return `<div class="aw-bar-item">
        <div class="aw-bar-label"><span class="aw-app-name">${esc(name)}</span><span class="aw-app-dur">${durationShort(app.seconds)}</span></div>
        <div class="aw-bar-track"><div class="aw-bar-fill" style="width:${pct}%"></div></div>
      </div>`;
    }).join("");
  } else {
    $("aw-top-apps").innerHTML = `<div class="muted">No application data yet.</div>`;
  }

  // Top titles
  const titles = aw.top_titles || [];
  if (titles.length) {
    $("aw-top-titles").innerHTML = titles.slice(0, 8).map(t => {
      const name = t.name.length > 55 ? t.name.slice(0, 52) + "…" : t.name;
      return `<div class="aw-title-item"><span class="aw-title-name">${esc(name)}</span><span class="aw-title-dur">${durationShort(t.seconds)}</span></div>`;
    }).join("");
  } else {
    $("aw-top-titles").innerHTML = `<div class="muted">No title data yet.</div>`;
  }
}

/* ═══ Main refresh ═══ */
async function refresh() {
  $("connection").textContent = "Refreshing…";
  try {
    const [health, summary, events, sessions, meaningful, classifications, behavior, interventions, outcomes, memories, regs, feed, effectiveness, breakState] = await Promise.all([
      get("/api/daemon/health"),
      get("/api/dashboard/summary"),
      get("/api/events?limit=30&sort=desc"),
      get("/api/sessions?limit=1"),
      get("/api/meaningful-sessions?limit=1"),
      get("/api/classifications?limit=1"),
      get("/api/behavior?limit=1"),
      get("/api/interventions?limit=1"),
      get("/api/outcomes?limit=1"),
      get("/api/memories?limit=1"),
      get("/api/browser/registrations"),
      get("/api/interventions/feed?limit=10").catch(() => ({ interventions: [] })),
      get("/api/responses/effectiveness").catch(() => ({ effectiveness: [] })),
      get("/api/break").catch(() => ({ break: { active: false } })),
    ]);

    renderInterventions(feed.interventions || []);
    renderEffectiveness(effectiveness.effectiveness || []);
    renderBreak(breakState.break || {});

    $("connection").textContent = "Local API connected";
    $("connection").className = "pill ok";
    $("daemon-dot").className = "dot live";

    // ── AW Summary Panel ──
    renderSourceSummary(summary.source_today);

    // ── Health panel ──
    const cfg = health.config || {};
    const bgAi = cfg.background_ai_enabled ? "Enabled" : "Off (on-demand)";
    $("health").innerHTML = rows([
      ["Status", health.status],
      ["Provider", health.provider || health.provider_name],
      ["Model", health.provider_model || health.model],
      ["Ingest cadence", cfg.ingest_interval_seconds ? `${cfg.ingest_interval_seconds}s` : "—"],
      ["Background AI", bgAi],
      ["Workers", Object.values(health.workers || {}).filter(w => w.status === "ok").length + " healthy"],
    ]);

    // ── Model badge ──
    const modelName = health.provider_model || cfg.ollama_model || "llama3.2:3b";
    $("model-badge").textContent = modelName.toUpperCase().replace(":", " · ");

    // ── Focus panel ──
    const b = behavior.observations?.[0] || {};
    const score = Math.round(Math.max(0, Math.min(1, (b.focus_score ?? (1 - (b.distraction_score ?? (b.actionable ? 0.75 : 0.28)))) )) * 100);
    $("focus-score").textContent = score + "%";
    $("focus-state").textContent = b.state || "Observing";
    $("focus-caption").textContent = b.actionable ? "An intervention may be useful now." : "Your activity currently looks stable.";
    $("focus-meter").style.width = score + "%";

    // ── Intelligence rows ──
    $("intelligence").innerHTML = rows([
      ["Latest behavior", b.state || "—"],
      ["Actionable", b.actionable == null ? "—" : b.actionable ? "Yes" : "No"],
      ["Meaningful sessions", (meaningful.sessions || []).length ? "available" : "none yet"],
      ["Stored memories", (memories.memories || []).length ? "available" : "none yet"],
      ["Privacy", "local SQLite"],
    ]);

    // ── Browser tab ──
    const reg = regs.registrations?.[0];
    $("tab").innerHTML = reg
      ? rows([["Browser", reg.browser], ["Device", reg.device_id || reg.device], ["Window", reg.current_window_id || "reported by bridge"], ["Tab", reg.current_tab_id || "reported by bridge"], ["Instance", reg.extension_instance_id]])
      : `<div class="muted">Install/reload the Firefox bridge to show the exact active tab.</div>`;

    const list = events.events || [];

    // ── Events table ──
    $("events").innerHTML = list.length
      ? list.map(e => `<tr><td>${esc((e.timestamp || e.time || "").replace("T", " ").slice(0, 19))}</td><td>${esc(e.app || e.application)}</td><td>${esc(e.domain || e.url || "")}</td><td>${duration(e.duration)}</td><td class="category">${esc(e.category || "Uncategorized")}</td><td><span class="signal-dot"></span>${esc(e.productivity || "Unclassified")}${e.evidence_quality ? ` · ${esc(e.evidence_quality)}` : ""}${e.classification_confidence != null ? ` · ${Math.round(Number(e.classification_confidence) * 100)}%` : ""}</td></tr>`).join("")
      : `<tr><td colspan="6" class="muted">No events yet. Start the collector and daemon.</td></tr>`;

    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $("connection").textContent = "API unavailable";
    $("connection").className = "pill bad";
    $("daemon-dot").className = "dot";
    $("health").innerHTML = `<div class="muted">${esc(e.message)}. Start the daemon with <code>python -m noema</code>.</div>`;
  }
}

/* ═══ Ollama controls ═══ */
async function runOllama() {
  const btn = $("run-ollama");
  const status = $("ollama-status");
  btn.disabled = true;
  btn.textContent = "⏳ Running…";
  status.textContent = "Classifying sessions…";
  try {
    const result = await post("/api/ai/run");
    const classified = result.classifications || result.meaningful_classifications || 0;
    status.textContent = `✓ Done — ${classified} sessions classified`;
    status.className = "ai-status-ok";
    await refresh();
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
    status.className = "ai-status-err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<span>✦</span> Run classification now`;
  }
}

async function stopOllama() {
  const btn = $("stop-ollama");
  const status = $("ollama-status");
  btn.disabled = true;
  btn.textContent = "Stopping…";
  try {
    const result = await post("/api/ollama/stop");
    status.textContent = result.stopped ? `✓ ${result.model} unloaded` : `⚠ ${result.message}`;
    status.className = result.stopped ? "ai-status-ok" : "ai-status-err";
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
    status.className = "ai-status-err";
  } finally {
    btn.disabled = false;
    btn.innerHTML = `Stop model <span>⏻</span>`;
  }
}

/* ═══ Tab categorize ═══ */
async function categorize() {
  try {
    const regs = (await get("/api/browser/registrations")).registrations || [];
    const r = regs[0];
    if (!r || !r.current_window_id || !r.current_tab_id) throw new Error("Firefox bridge is not reporting a tab");
    const result = await post("/api/browser/category", {
      device: r.device_id || r.device,
      browser: r.browser,
      window_id: r.current_window_id,
      tab_id: r.current_tab_id,
    });
    $("tab").innerHTML += `<div class="row"><span>Category</span><span class="category">${esc(result.category || result.status)}</span></div>`;
  } catch (e) { alert(e.message); }
}

/* ═══ Interventions feed (V3) ═══ */
function stateChip(item) {
  const bits = [item.status];
  if (item.delivered) bits.push("delivered");
  if (item.interacted) bits.push("acted");
  if (item.outcome) bits.push(item.outcome.recovery_status);
  return bits.map(esc).join(" · ");
}

async function interventionAction(id, action, body) {
  await post(`/api/interventions/${encodeURIComponent(id)}/action`,
    Object.assign({ action: action }, body || {}));
  await refresh();
}

async function interventionFeedback(id, feedbackType, value) {
  await post(`/api/interventions/${encodeURIComponent(id)}/feedback`,
    { feedback_type: feedbackType, value: value });
  await refresh();
}

async function interventionBreak(id) {
  await post(`/api/interventions/${encodeURIComponent(id)}/break`, { minutes: 5 });
  await refresh();
}

function renderInterventions(list) {
  const node = $("intervention-feed");
  if (!node) return;
  if (!list.length) {
    node.innerHTML = `<div class="muted">No interventions yet. Drift-triggered actions will appear here with their delivery state and outcome.</div>`;
    return;
  }
  node.innerHTML = list.map(item => {
    const msg = item.message || {};
    const outcome = item.outcome ? ` · outcome: ${esc(item.outcome.recovery_status)}${item.outcome.attribution ? ` (${esc(item.outcome.attribution)})` : ""}` : "";
    const feedback = (item.feedback || []).map(fb => `${esc(fb.feedback_type)}:${esc(fb.value)}`).join(", ");
    const open = item.status === "PLANNED" || item.status === "EXECUTED";
    return `<div class="row"><span><strong>${esc(msg.title || item.mode || "Noema")}</strong> — ${esc(msg.body || item.reason || "")}<br><small>${esc(item.created_at || "")} · ${stateChip(item)}${outcome}${feedback ? ` · feedback: ${feedback}` : ""}</small></span><span>${
      open
        ? `<button class="small-action" onclick="interventionAction('${esc(item.id)}','LOCK_IN')">Lock in</button> `
          + `<button class="small-action" onclick="interventionBreak('${esc(item.id)}')">5 min break</button> `
          + `<button class="small-action" onclick="interventionFeedback('${esc(item.id)}','INTERPRETATION','WRONG')">Intentional</button> `
          + `<button class="small-action" onclick="interventionFeedback('${esc(item.id)}','USEFULNESS','HELPFUL')">Helpful</button>`
        : ""
    }</span></div>`;
  }).join("");
}

function renderEffectiveness(table) {
  const node = $("response-table");
  if (!node) return;
  if (!table.length) {
    node.innerHTML = `<tr><td colspan="5" class="muted">No response data yet.</td></tr>`;
    return;
  }
  node.innerHTML = table.map(row =>
    `<tr><td>${esc(row.id)}</td><td>${esc(row.kind)}</td><td>${esc(row.times_shown)}</td>`
    + `<td>${esc(row.recovery_count)}</td>`
    + `<td>${row.recovery_rate == null ? "—" : Math.round(Number(row.recovery_rate) * 100) + "%"}</td></tr>`
  ).join("");
}

function renderBreak(info) {
  const node = $("break-badge");
  if (!node) return;
  node.textContent = info && info.active
    ? `On a break · back in ${Math.round(Number(info.remaining_seconds || 0) / 60)} min`
    : "";
}

/* ═══ Wire up ═══ */
$("refresh").onclick = refresh;
$("categorize").onclick = categorize;
$("run-ollama").onclick = runOllama;
$("stop-ollama").onclick = stopOllama;
$("reload")?.addEventListener("click", async () => { try { await post("/api/daemon/reload"); await refresh(); } catch (e) { alert(e.message); } });
$("cycle")?.addEventListener("click", async () => { try { await post("/api/autonomous/cycle"); await refresh(); } catch (e) { alert(e.message); } });
$("save-intent").onclick = async () => {
  try {
    const text = $("intent-input").value.trim();
    if (!text) return;
    await post("/api/intents", { text });
    $("intent-result").textContent = "Intent saved locally";
    $("intent-input").value = "";
  } catch (e) { $("intent-result").textContent = e.message; }
};

// Greeting based on time of day
(function() {
  const h = new Date().getHours();
  const greeting = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  const h1 = document.querySelector(".topbar h1");
  if (h1) h1.textContent = greeting + ".";
})();

refresh();
setInterval(refresh, 30000);
