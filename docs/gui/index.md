---
title: Bernstein web GUI - overview
description: Operator dashboard for Bernstein orchestration runs. Complements the Textual TUI with browser-based supervision.
tags:
  - gui
  - web
  - operator
---

# Bernstein web GUI

Browser-based operator surface for live Bernstein runs. Mounted on the same FastAPI process that serves `/api/v1/*`, exposed at `/ui/`.

## Quickstart

1. `bernstein gui serve` - binds `127.0.0.1:8052` and auto-opens `http://127.0.0.1:8052/ui/` in your browser.
2. On a loopback serve (`127.0.0.1`, `localhost`, or `::1`), including with `--no-open` or `--dev`, if `BERNSTEIN_AUTH_TOKEN` is unset (and auth is not disabled), the CLI reuses `.sdd/runtime/auth.token` when `.sdd/` already exists or mints an ephemeral bearer and exports it to the server process (persisting `.sdd/runtime/auth.token` at `0600` when `.sdd/` exists). On auto-open, it seeds your browser via a `#t=<token>` URL fragment that the SPA stores and scrubs from the address bar; with `--no-open` or `--dev` inside an interactive TTY or `.sdd/` workspace, the CLI prints how to authenticate (`BERNSTEIN_AUTH_TOKEN` / `.sdd/runtime/auth.token`) without dumping the raw API bearer to non-TTY logs (note that `_enforce_dashboard_posture()` still prints the separate `#2366` dashboard token when dashboard auth is unconfigured).
3. With `--tunnel` or any non-loopback bind, nothing is auto-minted (`--tunnel` uses its QR + passphrase onboarding, and non-loopback binds refuse to start without configured auth). `bernstein run` also exposes the live GUI URL (`/ui/`) alongside `.sdd/runtime/auth.token` and supports `--open-ui` on loopback.

## What it is

- Vite + React 19 + Tailwind 3 + shadcn/ui SPA. Source in `web/`. Built bundle ships in the wheel under `src/bernstein/gui/static/`.
- Nine operational screens: Tasks, Agents, Approvals, Audit, Governance, Costs, Missions, Fleet, Settings. Vocabulary is a separate reference page for shared UI states, outside this count; the root redirect and fallback are also excluded. Missions is the outcome-level timeline over a multi-day run: phase lanes, gate verdicts, envelope burn, and evidence links, each element tied to the receipt it was derived from. See [Screens](screens.md).
- The sidebar and command palette link to the server-rendered [review board](../operations/review-board.md) for attested merge-queue decisions.
- Reads from `GET /api/v1/*`, `GET /api/v1/events`, and `GET /api/v1/tasks/{task_id}/logs/stream`. Uses TanStack Query 5 for caching; SSE events trigger invalidate-and-refetch (`invalidateQueries` on the affected query keys) rather than writing payloads into the cache directly.
- Authenticated SSE: the SPA SSE client (`web/src/lib/sse.ts`) uses `fetch()` with `Authorization: Bearer ...` instead of browser-native `EventSource` (resolving `#3563`), so `/api/v1/events` works with auth enabled while maintaining interval polling fallback.
- Auth via `Authorization: Bearer ${localStorage.bernstein_token}`. Token comes from `BERNSTEIN_AUTH_TOKEN` (reused from `.sdd/runtime/auth.token` when initialized or auto-minted on loopback serve; `#2366` dashboard tokens apply only to `/dashboard*` and `/api/v1/dashboard/*`).

## When to use it

| You want to                                       | Use the GUI? |
|---------------------------------------------------|--------------|
| Watch a run live from a laptop browser            | yes          |
| Approve / deny tool calls with diff context       | yes          |
| Inspect HMAC audit chain head and verify          | yes          |
| Review per-adapter cost over the last 24 h        | yes          |
| Pipe Bernstein into a script or CI step           | no - use the REST API |
| Drive Bernstein from a terminal-only host         | no - use `bernstein live` (TUI) |

## Who it is for

Operators supervising live agent runs. The GUI mirrors what an operator already does in the TUI (`bernstein live`) but trades keyboard density for diff rendering, sparklines, and a queue-style approvals view.

## Read next

- [Install](install.md) - `pip install bernstein[gui]`, launch, ports.
- [Screens](screens.md) - what each route shows and which TUI widget it replaces.
- [Playground](playground.md) - zero-cost dev loop with `bernstein run --idle`.
- [Configuration](configuration.md) - env vars, auth token, theme, fleet toggle.
- [Mobile + tunnel](mobile.md) - installable PWA, `bernstein gui serve --tunnel`, QR onboarding (#1218).
- [Troubleshooting](troubleshooting.md) - common failure modes.
