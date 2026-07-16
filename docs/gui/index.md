---
title: Bernstein web GUI - overview
description: Operator dashboard for Bernstein orchestration runs. Replaces the Textual TUI for browser-based supervision.
tags:
  - gui
  - web
  - operator
---

# Bernstein web GUI

Browser-based operator surface for live Bernstein runs. Mounted on the same FastAPI process that serves `/api/v1/*`, exposed at `/ui/`.

## What it is

- Vite + React 18 + Tailwind 3 + shadcn/ui SPA. Source in `web/`. Built bundle ships in the wheel under `src/bernstein/gui/static/`.
- Seven routes: Tasks, Agents, Approvals, Audit, Costs, Fleet, Settings. One nav item each.
- Reads from `GET /api/v1/*` and the central SSE stream `GET /api/v1/events`. Uses TanStack Query 5 for caching and `setQueryData` to hydrate from SSE.
- Auth via `Authorization: Bearer ${localStorage.bernstein_token}`. Token comes from `BERNSTEIN_AUTH_TOKEN` (auto-generated on server launch if unset).

## When to use it

| You want to                                       | Use the GUI? |
|---------------------------------------------------|--------------|
| Watch a run live from a laptop browser            | yes          |
| Approve / deny tool calls with diff context       | yes          |
| Inspect HMAC audit chain head and verify          | yes          |
| Review per-adapter cost over the last 24 h        | yes          |
| Pipe Bernstein into a script or CI step           | no - use the REST API |
| Drive Bernstein from a terminal-only host         | no - use `bernstein dashboard` (TUI) |

## Who it is for

Operators supervising live agent runs. The GUI mirrors what an operator already does in the TUI (`bernstein dashboard`) but trades keyboard density for diff rendering, sparklines, and a queue-style approvals view.

## Read next

- [Install](install.md) - `pip install bernstein[gui]`, launch, ports.
- [Screens](screens.md) - what each route shows and which TUI widget it replaces.
- [Playground](playground.md) - zero-cost dev loop with `bernstein run --idle`.
- [Configuration](configuration.md) - env vars, auth token, theme, fleet toggle.
- [Mobile + tunnel](mobile.md) - installable PWA, `bernstein gui serve --tunnel`, QR onboarding (#1218).
- [Troubleshooting](troubleshooting.md) - common failure modes.
