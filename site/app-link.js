// Links this static page out to the real, local Noema daemon.
//
// This page never talks to the daemon's data endpoints (telemetry, sessions,
// interventions, memes) and never reimplements any of that UI — it only
// probes GET /health (documented in the API table above) to tell whether
// something is listening at the daemon's default address, so the "Open
// Noema" / "View live …" links can point at the real dashboard when it's
// there and fall back to the install instructions when it isn't, instead of
// handing the visitor a dead connection.
//
// The probe is best-effort: a page served over HTTPS may have this request
// blocked by the browser's private-network-access checks even when the
// daemon is actually running, in which case the links below simply fall
// back to "#developers" on click rather than misreporting a live status.
(function () {
  "use strict";

  var DAEMON_HEALTH_URL = "http://127.0.0.1:8765/health";
  var FALLBACK_SECTION_ID = "developers";
  var PROBE_TIMEOUT_MS = 1200;

  var appLinks = Array.prototype.slice.call(document.querySelectorAll("[data-app-link]"));
  var statusEl = document.querySelector("[data-app-status]");

  if (!appLinks.length) return;

  function redirectToInstall(event) {
    event.preventDefault();
    var target = document.getElementById(FALLBACK_SECTION_ID);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function setOnline(online) {
    appLinks.forEach(function (el) {
      el.classList.toggle("is-online", online);
      el.classList.toggle("is-offline", !online);
      if (!online) el.addEventListener("click", redirectToInstall);
    });
    if (statusEl) {
      statusEl.textContent = online
        ? "● LOCAL CORE ONLINE — detected at 127.0.0.1:8765"
        : "○ Not detected on this device — see “Run it locally” below";
      statusEl.classList.toggle("online", online);
      statusEl.classList.toggle("offline", !online);
    }
  }

  function probe() {
    if (typeof fetch !== "function" || typeof AbortController !== "function") return;
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, PROBE_TIMEOUT_MS);
    // mode: "no-cors" is deliberate: the daemon's CORS policy only reflects
    // loopback/extension origins (see NoemaApp._cors_origin in server.py), so
    // a cross-origin page can never read the response body or status. An
    // opaque no-cors fetch still resolves on any real HTTP response and
    // rejects on connection failure, which is exactly the "is it running"
    // signal this probe needs — it reads nothing back.
    fetch(DAEMON_HEALTH_URL, { mode: "no-cors", cache: "no-store", signal: controller.signal })
      .then(function () { clearTimeout(timer); setOnline(true); })
      .catch(function () { clearTimeout(timer); setOnline(false); });
  }

  probe();
})();
