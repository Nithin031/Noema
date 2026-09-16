# Noema Firefox extension

This WebExtension is a local delivery client. It complements the native
OS collectors with browser/tab semantics: it registers browser/window/tab
identity, receives targeted interventions, renders trusted overlays, and
reports user actions.

The popup keeps the existing dark Noema styling and now shows the local
daemon connection, collector ingestion state, provider health, current tab,
device, queued actions, event/session/meaningful-session counts, classifications,
behavior observations, interventions, outcomes, memories, personalization,
and a recent timeline. Refresh reads these values from the loopback API only.
The current tab also gets an exact category lookup by device, browser, window,
and tab identity. If it is still uncategorized, the popup's "Categorize current
tab" button requests one Ollama classification for that tab. The local API
endpoints are `GET /api/browser/category` for lookup and `POST
/api/browser/category` for an explicit classification request.

## Development install

1. Start the Noema local API with `serve(service)`. It exposes HTTP
   on `127.0.0.1:8765` and starts the preferred WebSocket bridge on
   `127.0.0.1:8766`.
2. Open `about:debugging#/runtime/this-firefox` in Firefox.
3. Choose **Load Temporary Add-on** and select this directory's
   `manifest.json`.

The extension registers at `/api/browser/register`, reports tab/window events,
and uses WebSocket delivery first. If the WebSocket bridge is unavailable it
uses the `/api/browser/poll` long-poll endpoint.

Only an exact `target.tab_id` (and, when supplied, `target.window_id`) is
eligible for an overlay. HOLDOUT actions are never delivered to this client.
Pages where content scripts cannot run use Firefox desktop notifications.
