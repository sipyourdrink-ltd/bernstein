# Bernstein web GUI

Vite + React + Tailwind + shadcn/ui SPA. Built artifacts ship inside the wheel under `src/bernstein/gui/static/`.

## Dev loop

Two terminals.

**Terminal 1** - Bernstein API server with mock orchestration:

```bash
# from repo root
cd /path/to/bernstein
bernstein run --idle    # spawns mock agents continuously
```

**Terminal 2** - Vite dev server (HMR + React Refresh, proxies `/api/*` to FastAPI on `:8000`):

```bash
cd web
npm install   # first time only
npm run dev
# open http://127.0.0.1:5173
```

Vite proxies `/api/*` to `http://127.0.0.1:8000` (where `bernstein gui serve --dev` or `bernstein run` is hosting FastAPI).

## Production build

```bash
cd web
npm run build
# emits to ../src/bernstein/gui/static/
```

The built `static/` directory is committed so `pip install bernstein[gui]` ships pre-built assets without requiring Node at install time.

## Serve built assets

```bash
pip install -e '.[gui]'
bernstein gui serve
# opens http://127.0.0.1:8000/ui/
```

## Layout

```
web/
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── tsconfig.json
├── index.html
└── src/
    ├── main.tsx              # React entry
    ├── App.tsx               # Router + providers
    ├── index.css             # Tailwind + shadcn token defaults
    ├── lib/
    │   ├── utils.ts          # cn() helper
    │   ├── api.ts            # fetch wrapper (TODO Phase 1.3)
    │   └── sse.ts            # useEventStream hook (TODO Phase 1.3)
    ├── components/
    │   ├── AppShell.tsx      # sidebar + topbar
    │   ├── ThemeProvider.tsx # dark/light/system
    │   └── PlaceholderScreen.tsx
    └── routes/               # Tasks, Agents, Approvals, Audit, Costs, Fleet, Settings, Overview
```

## Stack pin reasoning

- **Vite 6** - fast dev, no SSR overhead, builds to plain JS/CSS suitable for static serving.
- **React 18** - matches Bernstein's broader frontend ecosystem; React 19 deferred until shadcn/Radix peer-dep alignment lands.
- **Tailwind 3 + shadcn/ui** - operator can read raw classes without a brittle theme abstraction.
- **TanStack Query 5** - handles cache + retries + SSE refetches uniformly.
- **react-router 8** - `basename="/ui"` so the SPA works under FastAPI mount; declarative `BrowserRouter` only, imported from `react-router` (the `react-router-dom` shim is gone in v8).
