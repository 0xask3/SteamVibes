# Frontend

React 19 + TypeScript + Vite. One page: a search box, editable filter chips, a
result list, and a collapsed diagnostics panel.

```bash
npm install
npm run dev          # http://localhost:5173
npm run build        # tsc -b && vite build
npx tsc --noEmit     # typecheck alone
npm run lint         # oxlint - clean, and nothing in CI runs it
```

Open **`http://localhost:5173`**, not `127.0.0.1:5173`. Vite's dev server binds
IPv6 `::1` only, so the numeric address refuses the connection. The API is the
opposite — `src/api.ts` calls `127.0.0.1:8000`, because on Windows `localhost`
resolves to `::1` first and costs ~2.1s per new connection. Both spellings are
in the backend's CORS allowlist for exactly this reason.

It needs the backend on `:8000`. With nothing there the page renders and every
search reports a failed fetch.

## The four things worth knowing

**`VITE_API_URL` is baked in at build time**, not read at runtime — Vite inlines
`import.meta.env`, and the Dockerfile takes it as a build ARG. The default is
right for both `npm run dev` and the container, because the browser runs on the
host either way.

**Editing a chip does not re-parse.** Removing one posts the modified
`ParsedQuery` straight back as `SearchRequest.parsed`, and the backend skips the
chat model entirely — 0.095s against 1.347s. Re-parsing would also re-derive the
chip the user just deleted.

**Explanations are a second request.** Results render first, then `/api/explain`
fills in a line per card. `explain()` never throws: the list is correct without
them. A line the backend could not verify against the game's real tags arrives
with `grounded: false`, and `ResultCard.tsx` renders it differently and says so
on hover — a canned sentence presented as a real explanation is the exact
failure the verification exists to prevent.

**Relaxed filters are shown above the results, not below.** `response.parsed`
holds what was *actually* applied and `response.relaxed` is the diff. Silently
widening a constraint the user typed is worse than a short page.

## Files

| | |
| --- | --- |
| `App.tsx` | state, search + explain + stats orchestration, chip rendering |
| `ResultCard.tsx` | one game, including the grounded/fallback explanation |
| `StatsPanel.tsx` | collapsed `/api/stats` panel; withholds p95 below 21 samples |
| `chips.ts` | `ParsedQuery` → removable chips |
| `api.ts` | the three fetches; `explain` and `stats` swallow their own errors |
| `types.ts` | hand-mirrored from `backend/app/schemas.py` |

`types.ts` is written by hand rather than generated. There are four shapes and a
codegen step would be more machinery than it saves — but it does mean a schema
change in the backend needs the same edit here, and `tsc` will not catch it
because nothing validates the response at runtime.
