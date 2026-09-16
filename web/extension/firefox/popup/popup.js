(function () {
  "use strict";

  const BASE = "http://127.0.0.1:8765";
  const ids = ["daemon", "ingest", "provider", "events", "sessions", "meaningful",
    "classifications", "behavior", "interventions", "outcomes", "memories"];

  function text(id, value) {
    document.getElementById(id).textContent = value == null || value === "" ? "—" : String(value);
  }

  async function get(path) {
    const response = await fetch(BASE + path, { headers: { "Content-Type": "application/json" } });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }

  function records(payload, key) {
    return payload && Array.isArray(payload[key]) ? payload[key] : [];
  }

  function latestValue(payload, key, fallback) {
    const list = records(payload, key);
    const item = list.length ? list[list.length - 1] : null;
    return item && (item.state || item.status || item.category || item.productivity || item.recovery_status) || fallback;
  }

  function timelineItems(payload) {
    if (payload && Array.isArray(payload.items)) return payload.items;
    if (payload && Array.isArray(payload.timeline)) return payload.timeline;
    return [];
  }

  function renderState(state) {
    document.getElementById("connection").textContent = "Connection: " + (state.connection || "unknown");
    text("device", state.device_id);
    text("queued", state.queued || 0);
    const tab = state.current_tab;
    text("tab", tab && (tab.title || tab.url));
    return state;
  }

  function renderTabCategory(category) {
    text("tab-category", category && (category.category || "uncategorized"));
    text("tab-type", "Activity type: " + (category && category.activity_type || "—"));
    text("tab-productivity", "Productivity: " + (category && category.productivity || "—"));
    text("tab-topic", "Topic: " + (category && (category.topic || category.domain) || "—"));
    document.getElementById("categorize").textContent =
      category && category.status === "pending" ? "Waiting for Ollama" : "Categorize current tab";
  }

  function renderData(data) {
    const health = data.health || {};
    const ingest = health.workers && health.workers.ingest || {};
    text("daemon", health.status);
    text("ingest", ingest.status || "—");
    text("provider", (health.provider || health.config && health.config.provider) +
      (health.provider_health ? " / " + health.provider_health : ""));
    text("events", records(data.events, "events").length);
    text("sessions", records(data.sessions, "sessions").length);
    text("meaningful", records(data.meaningful, "sessions").length);
    text("classifications", records(data.classifications, "classifications").length);
    text("behavior", records(data.behavior, "observations").length);
    text("interventions", records(data.interventions, "interventions").length);
    text("outcomes", records(data.outcomes, "outcomes").length);
    text("memories", records(data.memories, "memories").length);
    text("behavior-detail", latestValue(data.behavior, "observations", "No state yet"));
    text("classification-detail", latestValue(data.classifications, "classifications", "No label yet"));
    text("intervention-detail", latestValue(data.interventions, "interventions", "No action yet"));
    text("outcome-detail", latestValue(data.outcomes, "outcomes", "No outcome yet"));
    const profile = data.personalization || {};
    text("personalization", profile.preferred_mode || profile.preference || profile.status || "No profile yet");
    const list = document.getElementById("timeline");
    list.textContent = "";
    const items = timelineItems(data.timeline).slice(-8).reverse();
    if (!items.length) { list.innerHTML = "<li>No recent timeline data</li>"; }
    items.forEach(function (item) {
      const li = document.createElement("li");
      li.textContent = item.title || item.app || item.domain || item.type || item.state || "Activity record";
      list.appendChild(li);
    });
  }

  async function refresh() {
    const button = document.getElementById("refresh");
    button.disabled = true;
    try {
      const state = await browser.runtime.sendMessage({ type: "GET_STATE" });
      renderState(state);
      const categoryPath = state.current_window_id && state.current_tab_id
        ? "/api/browser/category?device=" + encodeURIComponent(state.device_id || "") +
          "&browser=firefox&window_id=" + encodeURIComponent(state.current_window_id) +
          "&tab_id=" + encodeURIComponent(state.current_tab_id)
        : null;
      const paths = ["/api/daemon/health", "/api/events?limit=1000", "/api/sessions?limit=1000",
        "/api/meaningful-sessions?limit=1000", "/api/classifications?limit=1000",
        "/api/behavior?limit=1000", "/api/interventions?limit=1000", "/api/outcomes?limit=1000",
        "/api/memories?limit=1000", "/api/personalization", "/api/timeline?limit=25", categoryPath];
      const values = await Promise.all(paths.map(function (path) {
        return path ? get(path).catch(function () { return {}; }) : Promise.resolve({});
      }));
      renderData({ health: values[0], events: values[1], sessions: values[2], meaningful: values[3],
        classifications: values[4], behavior: values[5], interventions: values[6], outcomes: values[7],
        memories: values[8], personalization: values[9], timeline: values[10] });
      renderTabCategory(values[11]);
      document.getElementById("updated").textContent = "Updated " + new Date().toLocaleTimeString();
    } catch {
      document.getElementById("connection").textContent = "Connection: unavailable";
      ids.forEach(function (id) { if (id !== "queued") text(id, "—"); });
      document.getElementById("timeline").innerHTML = "<li>Start the local daemon to load activity</li>";
      renderTabCategory({ status: "uncategorized", category: "uncategorized" });
    } finally { button.disabled = false; }
  }
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("categorize").addEventListener("click", async function () {
    const button = document.getElementById("categorize");
    button.disabled = true;
    try {
      const state = await browser.runtime.sendMessage({ type: "GET_STATE" });
      if (!state.current_window_id || !state.current_tab_id) return;
      const category = await fetch(BASE + "/api/browser/category", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device: state.device_id, browser: "firefox",
          window_id: state.current_window_id, tab_id: state.current_tab_id })
      }).then(function (response) { return response.json(); });
      renderTabCategory(category);
    } catch { renderTabCategory({ status: "uncategorized", category: "unavailable" }); }
    finally { button.disabled = false; }
  });
  refresh();
}());
