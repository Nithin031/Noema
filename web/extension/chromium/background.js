/* Noema bridge for Brave and Edge (Manifest V3 service worker).
 * Reports the active tab (url + title) to the local daemon and shows its nudges.
 * Chrome is deliberately ignored; install this only in the browsers you want tracked. */
/* global importScripts, AIActivityOSApi */
importScripts("api/client.js");

(function () {
  "use strict";

  const api = AIActivityOSApi;
  const state = {
    browser: null, deviceId: null, instanceId: null, currentWindowId: null,
    currentTabId: null, currentTab: null, connection: "starting", queued: [],
  };
  const notificationInterventions = new Map();
  let polling = false;
  let ready = null;

  function randomId(prefix) {
    return prefix + (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2));
  }

  async function detectBrowser() {
    try { if (navigator.brave && (await navigator.brave.isBrave())) return "brave"; } catch { /* not brave */ }
    const brands = (navigator.userAgentData && navigator.userAgentData.brands || []).map(function (b) { return b.brand; }).join(" ");
    if (/Microsoft Edge/i.test(brands) || /\sEdg\//.test(navigator.userAgent)) return "edge";
    return "chrome"; // not tracked
  }

  // The service worker can be stopped at any time, so identity lives in storage.
  async function initialize() {
    state.browser = await detectBrowser();
    const stored = await chrome.storage.local.get(["device_id", "instance_id"]);
    state.deviceId = stored.device_id || "laptop-01";
    state.instanceId = stored.instance_id || randomId(state.browser + "-");
    await chrome.storage.local.set({ device_id: state.deviceId, instance_id: state.instanceId });
    globalThis.NoemaBrowser = state.browser;
    globalThis.NoemaDeviceId = state.deviceId;
    globalThis.NoemaInstanceId = state.instanceId;
  }

  function tracked() { return state.browser === "brave" || state.browser === "edge"; }

  function eventPayload(eventType, tab, windowId) {
    return {
      event_type: eventType,
      window_id: windowId == null ? null : String(windowId),
      tab_id: tab && tab.id != null ? String(tab.id) : null,
      url: tab && tab.url ? tab.url : null,
      title: tab && tab.title ? tab.title : null,
      private: tab && typeof tab.incognito === "boolean" ? tab.incognito : null,
      timestamp: new Date().toISOString(),
    };
  }

  // Browser-internal pages (edge://extensions, brave://settings, new tab, ...) are never reported.
  function internalPage(url) { return /^(chrome|edge|brave|about|chrome-extension|devtools|view-source):/i.test(String(url || "")); }

  async function reportEvent(event) {
    if (internalPage(event.url)) return;
    try { await api.event(state.instanceId, event); } catch { /* the poll loop re-registers */ }
  }

  async function refreshCurrentTab(reason) {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const tab = tabs && tabs.length ? tabs[0] : null;
    state.currentTab = tab;
    state.currentTabId = tab && tab.id != null ? String(tab.id) : null;
    state.currentWindowId = tab && tab.windowId != null ? String(tab.windowId) : null;
    await reportEvent(eventPayload(reason || "tab_activated", tab, state.currentWindowId));
    await drainQueue();
  }

  function targetMatches(target) {
    target = target || {};
    if (!target.tab_id || String(target.tab_id) !== String(state.currentTabId)) return false;
    if (target.window_id && String(target.window_id) !== String(state.currentWindowId)) return false;
    if (target.device && String(target.device) !== String(state.deviceId)) return false;
    if (target.browser && String(target.browser).toLowerCase() !== state.browser) return false;
    return true;
  }

  function injectable(tab) {
    const url = tab && tab.url ? String(tab.url) : "";
    if (!url || /^(chrome|edge|brave|about|chrome-extension|view-source|file):/i.test(url)) return false;
    return !/\.pdf(?:$|[?#])/i.test(url);
  }

  function messageFor(payload) {
    return payload.message || { title: "Noema", body: "You may be drifting from your current goal." };
  }

  async function reportState(payload, stateName, metadata) {
    try {
      await api.state(payload.intervention_id, { state: stateName, target: payload.target || {}, metadata: metadata || {} });
    } catch { /* backend may be temporarily unavailable */ }
  }

  async function reportAction(payload, action) {
    const metadata = { browser: state.browser, tab_id: state.currentTabId, action: action };
    if (action === "BREAK_5MIN") metadata.minutes = 5;
    try {
      await api.action(payload.intervention_id, { action: "CLICKED", state: "INTERACTED", target: payload.target || {}, metadata: metadata });
      await api.action(payload.intervention_id, { action: action, state: "INTERACTED", target: payload.target || {}, metadata: metadata });
    } catch { /* interaction stays local if the backend is down */ }
  }

  async function desktopFallback(payload, reason) {
    const message = messageFor(payload);
    const id = "noema-" + payload.intervention_id;
    notificationInterventions.set(id, payload);
    try {
      await chrome.notifications.create(id, {
        type: "basic", title: message.title || "Noema",
        message: message.body || "You may be drifting from your current goal.",
        iconUrl: chrome.runtime.getURL("assets/icon-128.png"),
      });
      await reportState(payload, "DISPLAYED", { channel: "desktop_notification", reason: reason });
    } catch {
      await reportState(payload, "AUTO_DISMISSED", { channel: "unavailable", reason: reason });
    }
  }

  async function deliver(payload) {
    await reportState(payload, "RECEIVED");
    if (!targetMatches(payload.target)) {
      state.queued.push(payload);
      await desktopFallback(payload, "target_tab_not_active");
      return;
    }
    if (!injectable(state.currentTab)) { await desktopFallback(payload, "page_not_injectable"); return; }
    try {
      await chrome.tabs.sendMessage(Number(state.currentTabId), { type: "AI_ACTIVITY_INTERVENTION", payload: payload });
      await reportState(payload, "DISPLAYED", { channel: "shadow_dom_overlay" });
    } catch {
      await desktopFallback(payload, "content_script_unavailable");
    }
  }

  async function drainQueue() {
    const remaining = [];
    for (const payload of state.queued) {
      if (targetMatches(payload.target)) await deliver(payload); else remaining.push(payload);
    }
    state.queued = remaining;
  }

  async function receive(message) {
    if (message && message.type === "intervention" && message.payload) await deliver(message.payload);
  }

  // Long-poll the daemon; each response resets the service worker's idle timer,
  // and the alarm below restarts the loop if the worker was stopped.
  async function startPolling() {
    if (polling || !tracked()) return;
    polling = true;
    try {
      for (;;) {
        try {
          const response = await api.poll(state.instanceId, 20);
          state.connection = "polling";
          for (const message of (response.messages || [])) await receive(message);
        } catch {
          state.connection = "offline";
          await new Promise(function (resolve) { setTimeout(resolve, 3000); });
          try { await api.register({ browser: state.browser, device_id: state.deviceId, extension_instance_id: state.instanceId }); } catch { /* retried */ }
        }
      }
    } finally { polling = false; }
  }

  async function start() {
    await initialize();
    if (!tracked()) { state.connection = "ignored (not Brave or Edge)"; return; }
    try {
      await api.register({ browser: state.browser, device_id: state.deviceId, extension_instance_id: state.instanceId });
      state.connection = "registered";
      await refreshCurrentTab("window_focused");
    } catch { state.connection = "offline"; }
    startPolling();
  }

  function whenReady(fn) {
    return function () {
      const args = arguments;
      (ready || (ready = start())).then(function () { if (tracked()) fn.apply(null, args); });
    };
  }

  chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
    if (!message) return false;
    if (message.type === "GET_STATE") {
      (ready || (ready = start())).then(function () {
        sendResponse({
          browser: state.browser, device_id: state.deviceId, connection: state.connection,
          current_window_id: state.currentWindowId, current_tab_id: state.currentTabId,
          current_tab: state.currentTab, queued: state.queued.length,
        });
      });
      return true;
    }
    if ((message.type === "intervention-state" || message.type === "intervention-action") && message.payload) {
      const work = message.type === "intervention-action" || message.state === "INTERACTED"
        ? reportAction(message.payload, message.action)
        : reportState(message.payload, message.state || "DISPLAYED", { channel: "shadow_dom_overlay" });
      work.then(function () { sendResponse({ ok: true }); });
      return true;
    }
    return false;
  });

  chrome.notifications.onClicked.addListener(whenReady(function (id) {
    const payload = notificationInterventions.get(id);
    if (payload) reportAction(payload, "LOCK_IN");
  }));
  chrome.tabs.onActivated.addListener(whenReady(function () { refreshCurrentTab("tab_activated"); }));
  chrome.windows.onFocusChanged.addListener(whenReady(function (windowId) {
    if (windowId !== chrome.windows.WINDOW_ID_NONE) refreshCurrentTab("window_focused");
  }));
  chrome.tabs.onCreated.addListener(whenReady(function (tab) { reportEvent(eventPayload("tab_created", tab, tab.windowId)); }));
  chrome.tabs.onRemoved.addListener(whenReady(function (tabId, info) {
    reportEvent(eventPayload("tab_closed", { id: tabId }, info.windowId));
    if (String(tabId) === String(state.currentTabId)) refreshCurrentTab("tab_activated");
  }));
  chrome.tabs.onUpdated.addListener(whenReady(function (tabId, changeInfo, tab) {
    if (changeInfo.status === "complete" || changeInfo.url) {
      reportEvent(eventPayload("navigation_changed", tab, tab.windowId));
      if (String(tabId) === String(state.currentTabId)) { state.currentTab = tab; drainQueue(); }
    }
  }));

  // Wake the worker regularly so polling resumes after Chromium stops it.
  chrome.alarms.create("noema-keepalive", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener(whenReady(function () { startPolling(); }));
  chrome.runtime.onStartup.addListener(function () { ready = ready || start(); });
  chrome.runtime.onInstalled.addListener(function () { ready = ready || start(); });
  ready = start();
}());
