/* global AIActivityOSApi: readonly */
(function () {
  "use strict";

  const api = AIActivityOSApi;
  const state = {
    browser: "firefox",
    deviceId: null,
    instanceId: null,
    currentWindowId: null,
    currentTabId: null,
    currentTab: null,
    connection: "starting",
    queued: []
  };
  let socket = null;
  let pollLoopRunning = false;
  const notificationInterventions = new Map();

  function randomId(prefix) {
    if (globalThis.crypto && crypto.randomUUID) return prefix + crypto.randomUUID();
    return prefix + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  async function initializeIdentity() {
    const stored = await browser.storage.local.get(["device_id"]);
    state.deviceId = stored.device_id || "laptop-01";
    await browser.storage.local.set({ device_id: state.deviceId });
    state.instanceId = randomId("firefox-");
    globalThis.NoemaDeviceId = state.deviceId;
    globalThis.NoemaInstanceId = state.instanceId;
  }

  function eventPayload(eventType, tab, windowId, extra) {
    const data = Object.assign({
      event_type: eventType,
      window_id: windowId == null ? null : String(windowId),
      tab_id: tab && tab.id != null ? String(tab.id) : null,
      url: tab && tab.url ? tab.url : null,
      title: tab && tab.title ? tab.title : null,
      private: tab && typeof tab.incognito === "boolean" ? tab.incognito : null,
      timestamp: new Date().toISOString()
    }, extra || {});
    return data;
  }

  async function reportEvent(event) {
    try { await api.event(state.instanceId, event); } catch { /* reconnect loop will retry */ }
  }

  async function refreshCurrentTab(reason) {
    const tabs = await browser.tabs.query({ active: true, lastFocusedWindow: true });
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
    if (target.browser && String(target.browser).toLowerCase() !== "firefox") return false;
    return true;
  }

  function injectable(tab) {
    const url = tab && tab.url ? String(tab.url) : "";
    if (!url || /^(about:|moz-extension:|view-source:|file:)/i.test(url)) return false;
    if (/\.pdf(?:$|[?#])/i.test(url)) return false;
    return true;
  }

  function messageFor(payload) {
    return payload.message || {
      title: payload.type === "MEME" ? "BRO 💀" : "Noema",
      body: "You may be drifting from your current goal."
    };
  }

  async function reportState(payload, stateName, metadata) {
    try {
      await api.state(payload.intervention_id, {
        state: stateName,
        target: payload.target || {},
        metadata: metadata || {}
      });
    } catch { /* backend may be temporarily unavailable */ }
  }

  async function reportAction(payload, action) {
    const metadata = { browser: "firefox", tab_id: state.currentTabId, action: action };
    // V3 typed actions carry backend-relevant extras: break length for the
    // intentional-break state, nothing extra for lock-in/intentional.
    if (action === "BREAK_5MIN") metadata.minutes = 5;
    try {
      await api.action(payload.intervention_id, {
        action: "CLICKED",
        state: "INTERACTED",
        target: payload.target || {},
        metadata: metadata
      });
      await api.action(payload.intervention_id, {
        action: action,
        state: "INTERACTED",
        target: payload.target || {},
        metadata: metadata
      });
    } catch { /* interaction remains local if backend is unavailable */ }
  }

  async function desktopFallback(payload, reason) {
    const message = messageFor(payload);
    const id = "noema-" + payload.intervention_id;
    notificationInterventions.set(id, payload);
    try {
      await browser.notifications.create(id, {
        type: "basic",
        title: message.title || "Noema",
        message: message.body || "You may be drifting from your current goal.",
        iconUrl: browser.runtime.getURL("assets/icon.svg")
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
      return { delivered: false, queued: true };
    }
    if (!injectable(state.currentTab)) {
      await desktopFallback(payload, "page_not_injectable");
      return { delivered: false, fallback: true };
    }
    try {
      await browser.tabs.sendMessage(Number(state.currentTabId), {
        type: "AI_ACTIVITY_INTERVENTION",
        payload: payload
      });
      await reportState(payload, "DISPLAYED", { channel: "shadow_dom_overlay" });
      return { delivered: true };
    } catch {
      await desktopFallback(payload, "content_script_unavailable");
      return { delivered: false, fallback: true };
    }
  }

  async function drainQueue() {
    const remaining = [];
    for (const payload of state.queued) {
      if (targetMatches(payload.target)) await deliver(payload);
      else remaining.push(payload);
    }
    state.queued = remaining;
  }

  async function receive(message) {
    if (!message || message.type !== "intervention" || !message.payload) return;
    await deliver(message.payload);
  }

  function connect() {
    if (!state.instanceId) return;
    state.connection = "connecting";
    socket = api.connectWebSocket(
      "ws://127.0.0.1:8766/api/browser/ws",
      function (message) {
        if (message.type === "registered") state.connection = "websocket";
        receive(message);
      },
      function () {
        state.connection = "polling";
        socket = null;
        startPolling();
      }
    );
    if (!socket) startPolling();
  }

  async function startPolling() {
    if (pollLoopRunning) return;
    pollLoopRunning = true;
    while (!socket) {
      try {
        const response = await api.poll(state.instanceId, 20);
        state.connection = "polling";
        for (const message of (response.messages || [])) await receive(message);
      } catch {
        state.connection = "offline";
        await new Promise(resolve => setTimeout(resolve, 3000));
        try { await api.register({ browser: "firefox", device_id: state.deviceId, extension_instance_id: state.instanceId }); } catch { /* registration retried on next poll iteration */ }
      }
    }
    pollLoopRunning = false;
  }

  browser.runtime.onMessage.addListener(function (message) {
    if (!message) return undefined;
    if (message.type === "intervention-state" && message.payload) {
      if (message.state === "INTERACTED") return reportAction(message.payload, message.action);
      return reportState(message.payload, message.state || "DISPLAYED", { channel: "shadow_dom_overlay" });
    }
    if (message.type === "intervention-action" && message.payload) {
      return reportAction(message.payload, message.action);
    }
    if (message.type === "GET_STATE") {
      return Promise.resolve({
        browser: state.browser,
        device_id: state.deviceId,
        connection: state.connection,
        current_window_id: state.currentWindowId,
        current_tab_id: state.currentTabId,
        current_tab: state.currentTab,
        queued: state.queued.length
      });
    }
    return undefined;
  });

  browser.notifications.onClicked.addListener(async function (notificationId) {
    const payload = notificationInterventions.get(notificationId);
    if (payload) await reportAction(payload, "LOCK_IN");
  });

  browser.tabs.onActivated.addListener(function () {
    refreshCurrentTab("tab_activated");
  });
  browser.windows.onFocusChanged.addListener(function (windowId) {
    if (windowId !== browser.windows.WINDOW_ID_NONE) refreshCurrentTab("window_focused");
  });
  browser.tabs.onCreated.addListener(function (tab) {
    reportEvent(eventPayload("tab_created", tab, tab.windowId));
  });
  browser.tabs.onRemoved.addListener(function (tabId, removeInfo) {
    reportEvent(eventPayload("tab_closed", { id: tabId }, removeInfo.windowId));
    if (String(tabId) === String(state.currentTabId)) refreshCurrentTab("tab_activated");
  });
  browser.tabs.onUpdated.addListener(function (tabId, changeInfo, tab) {
    if (changeInfo.status === "complete" || changeInfo.url) {
      reportEvent(eventPayload("navigation_changed", tab, tab.windowId));
      if (String(tabId) === String(state.currentTabId)) {
        state.currentTab = tab;
        drainQueue();
      }
    }
  });

  (async function start() {
    await initializeIdentity();
    try {
      await api.register({
        browser: "firefox",
        device_id: state.deviceId,
        extension_instance_id: state.instanceId
      });
      state.connection = "registered";
      connect();
      await refreshCurrentTab("window_focused");
    } catch {
      state.connection = "offline";
      startPolling();
    }
  }());
}());
