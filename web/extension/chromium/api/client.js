(function (global) {
  "use strict";

  const DEFAULT_HTTP_BASE = "http://127.0.0.1:8765";

  async function request(path, options) {
    const response = await fetch(DEFAULT_HTTP_BASE + path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, options || {}));
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch (error) {
      throw new Error("Noema returned invalid JSON", { cause: error });
    }
    if (!response.ok) {
      throw new Error(data.error || ("Noema HTTP " + response.status));
    }
    return data;
  }

  function post(path, body) {
    return request(path, { method: "POST", body: JSON.stringify(body || {}) });
  }

  function connectWebSocket(url, onMessage, onClose) {
    let socket;
    try { socket = new WebSocket(url); } catch (error) {
      if (onClose) onClose(error);
      return null;
    }
    socket.addEventListener("open", function () {
      socket.send(JSON.stringify({
        type: "register",
        browser: global.NoemaBrowser || "chromium",
        device_id: global.NoemaDeviceId,
        extension_instance_id: global.NoemaInstanceId
      }));
    });
    socket.addEventListener("message", function (event) {
      try { if (onMessage) onMessage(JSON.parse(event.data)); } catch { /* ignore malformed server data */ }
    });
    socket.addEventListener("close", function (event) { if (onClose) onClose(event); });
    socket.addEventListener("error", function (event) { if (onClose) onClose(event); });
    return socket;
  }

  global.AIActivityOSApi = {
    register: function (payload) { return post("/api/browser/register", payload); },
    event: function (instanceId, event) {
      return post("/api/browser/event", { extension_instance_id: instanceId, event: event });
    },
    action: function (interventionId, payload) {
      return post("/api/interventions/" + encodeURIComponent(interventionId) + "/action", payload);
    },
    state: function (interventionId, payload) {
      return post("/api/interventions/" + encodeURIComponent(interventionId) + "/state", payload);
    },
    poll: function (instanceId, timeoutSeconds) {
      const query = "?extension_instance_id=" + encodeURIComponent(instanceId) +
        "&timeout=" + encodeURIComponent(timeoutSeconds || 0);
      return request("/api/browser/poll" + query);
    },
    connectWebSocket: connectWebSocket
  };
}(this));
