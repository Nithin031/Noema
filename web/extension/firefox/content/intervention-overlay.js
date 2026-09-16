(function () {
  "use strict";

  let root = null;
  let timer = null;
  let countdownTimer = null;

  const CSS = `
    :host { all: initial; }
    #backdrop { position: fixed; inset: 0; z-index: 2147483647; display: flex;
      align-items: center; justify-content: center; background: rgba(0,0,0,.55);
      font-family: system-ui, -apple-system, sans-serif; pointer-events: auto; }
    #card { width: min(560px, calc(100vw - 32px)); color: #f8fafc; background: #111827;
      border: 1px solid rgba(255,255,255,.16); border-radius: 20px; padding: 28px;
      box-shadow: 0 24px 80px rgba(0,0,0,.5); text-align: center; }
    #card.gentle { width: min(420px, calc(100vw - 32px)); padding: 20px; text-align: left; }
    #asset { overflow: hidden; border-radius: 12px; margin: 0 auto 18px; }
    #asset svg, #asset img { display: block; width: 100%; height: auto; }
    #title { font-size: 28px; font-weight: 800; margin: 0 0 12px; }
    #body { color: #cbd5e1; font-size: 16px; line-height: 1.45; margin: 0 0 18px; }
    #goal { color: #93c5fd; font-size: 14px; margin: 8px 0 18px; }
    #context { color: #64748b; font-size: 13px; margin: 0 0 18px; }
    #meme { overflow: hidden; border-radius: 12px; margin: 0 auto 18px; }
    #meme svg { display: block; width: 100%; height: auto; }
    #actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; }
    button { border: 0; border-radius: 999px; padding: 11px 17px; cursor: pointer;
      color: #0f172a; background: #fbbf24; font: 700 14px system-ui; }
    button.secondary { color: #e2e8f0; background: #334155; }
    button.close { color: #cbd5e1; background: transparent; }
    #expiry { color: #64748b; font-size: 12px; margin-top: 14px; }
  `;

  function send(payload, state, action) {
    return browser.runtime.sendMessage({
      type: action ? "intervention-action" : "intervention-state",
      payload: payload,
      state: state,
      action: action
    });
  }

  function remove(payload, state, expectedRoot) {
    if (expectedRoot && root !== expectedRoot) return;
    if (timer) clearTimeout(timer);
    timer = null;
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = null;
    if (root) root.remove();
    root = null;
    if (state === "AUTO_DISMISSED") send(payload, "INTERACTED", "IGNORED");
    send(payload, state || "DISMISSED");
  }

  function button(label, action, className, payload) {
    const item = document.createElement("button");
    item.textContent = label;
    if (className) item.className = className;
    item.addEventListener("click", function () {
      send(payload, "INTERACTED", action);
      remove(payload, "DISMISSED");
    });
    return item;
  }

  function startCountdown(shadow, payload, expiresIn) {
    const total = Math.max(5000, Math.min(Number(expiresIn || 5000), 300000));
    const node = shadow.querySelector("#expiry");
    const deadline = Date.now() + total;
    function tick() {
      const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
      if (node) node.textContent = "Closing in " + left + "s";
      if (left <= 0 && countdownTimer) clearInterval(countdownTimer);
    }
    tick();
    countdownTimer = setInterval(tick, 1000);
    return total;
  }

  function createOverlay(payload) {
    if (timer) clearTimeout(timer);
    timer = null;
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = null;
    if (root) root.remove();
    root = document.createElement("div");
    root.id = "noema-intervention-root";
    const shadow = root.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);

    const backdrop = document.createElement("div");
    backdrop.id = "backdrop";
    const card = document.createElement("section");
    card.id = "card";
    if (payload.type === "NOTIFICATION") card.className = "gentle";
    const message = payload.message || {};
    // V3 response asset slot (curated SVG). Model output is never trusted
    // as markup: only <svg> without scripts/handlers renders as an image.
    // Phase 10 dataset images arrive as a loopback image_url (host
    // permissions already cover 127.0.0.1:8765); only http(s) loopback
    // URLs are accepted, never arbitrary remote sources.
    const responseKind = payload.response_kind || (payload.meme ? "MEME" : null);
    if (responseKind && (payload.svg_asset || payload.svg || payload.image_url)) {
      const asset = document.createElement("div");
      asset.id = "asset";
      const imageUrl = typeof payload.image_url === "string" ? payload.image_url : "";
      if (/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(imageUrl)) {
        const image = document.createElement("img");
        image.alt = "Noema response";
        image.src = imageUrl;
        asset.appendChild(image);
        card.appendChild(asset);
      } else {
        const svgText = payload.svg || "";
        if (/^\s*<svg[\s>]/i.test(svgText) && !/<script|on\w+\s*=/i.test(svgText)) {
          const image = document.createElement("img");
          image.alt = "Noema response";
          image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgText);
          asset.appendChild(image);
          card.appendChild(asset);
        }
      }
    }
    const title = document.createElement("h2");
    title.id = "title";
    title.textContent = message.title || (payload.type === "MEME" ? "BRO" : "Noema");
    card.appendChild(title);
    const body = document.createElement("p");
    body.id = "body";
    body.textContent = message.body || "You may be drifting from your current task.";
    card.appendChild(body);
    if (payload.meme && globalThis.AIMemeRenderer && !payload.svg) {
      const meme = document.createElement("div");
      meme.id = "meme";
      globalThis.AIMemeRenderer.render(meme, payload.meme);
      card.appendChild(meme);
    }
    if (payload.goal) {
      const goal = document.createElement("p");
      goal.id = "goal";
      goal.textContent = "Goal: " + payload.goal;
      card.appendChild(goal);
    }
    if (payload.context_line) {
      const context = document.createElement("p");
      context.id = "context";
      context.textContent = payload.context_line;
      card.appendChild(context);
    }
    const actions = document.createElement("div");
    actions.id = "actions";
    // Typed V3 actions: LOCK IN (recover), 5 MIN BREAK (intentional pause),
    // THIS IS INTENTIONAL (reject interpretation). No generic dismiss: the
    // card expires on its own and expiry is recorded as ignored.
    actions.appendChild(button("LOCK IN", "LOCK_IN", "", payload));
    actions.appendChild(button("5 MIN BREAK", "BREAK_5MIN", "secondary", payload));
    actions.appendChild(button("THIS IS INTENTIONAL", "INTENTIONAL", "close", payload));
    card.appendChild(actions);
    const expiry = document.createElement("div");
    expiry.id = "expiry";
    card.appendChild(expiry);
    backdrop.appendChild(card);
    shadow.appendChild(backdrop);
    document.documentElement.appendChild(root);
    const overlayRoot = root;
    send(payload, "DISPLAYED");
    setTimeout(function () {
      if (root === overlayRoot) send(payload, "SEEN");
    }, 250);
    const timeout = startCountdown(shadow, payload, payload.expires_in);
    timer = setTimeout(function () {
      remove(payload, "AUTO_DISMISSED", overlayRoot);
    }, timeout);
  }

  browser.runtime.onMessage.addListener(function (message) {
    if (message && message.type === "AI_ACTIVITY_INTERVENTION" && message.payload) {
      createOverlay(message.payload);
      return Promise.resolve({ displayed: true });
    }
    return undefined;
  });
}());
