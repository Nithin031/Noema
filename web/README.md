# Noema web

The React app the Noema daemon serves at `http://127.0.0.1:8765/` (riso zine control room).

```bash
npm install
npm run dev      # hot reload; talks to the daemon on 127.0.0.1:8765
npm run build    # tsc + vite build -> dist/ (served by the daemon)
npm run lint
npm test
```

- `src/styles/tokens.css` - design tokens (authored at 3840px wide; `components/Stage.tsx` zooms to fit).
- `src/api/noema.ts` - typed API client (same-origin in production).
- `src/lib/adapt.ts` - backend payload -> screen view-model mapping (unit-tested).
- `src/hooks/useLive.tsx` - one shared set of pollers for the whole app.
- `src/screens/` - one file per screen; `src/components/` - Rope, Gauge, Nav, primitives.
- `extension/chromium/` - the Brave/Edge browser bridge and in-page nudge overlay (separate from this app).

Fonts are bundled locally (`@fontsource`), so the app makes no external requests.
