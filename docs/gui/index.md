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
2. On a plain loopback serve with auto-open, if `BERNSTEIN_AUTH_TOKEN` is unset (and auth is not disabled) the CLI mints an ephemeral bearer, exports it to the server process, and seeds your browser with it via a URL fragment the SPA scrubs after capture - no token to copy.
3. With `--no-open`, `--dev`, or `--tunnel` nothing is auto-minted: set `BERNSTEIN_AUTH_TOKEN` yourself and paste it into the SPA's token screen (`--tunnel` has its own QR + passphrase onboarding). Non-loopback binds never auto-mint and refuse to start without configured auth.

## What it is

- Vite + React 19 + Tailwind 3 + shadcn/ui SPA. Source in `web/`. Built bundle ships in the wheel under `src/bernstein/gui/static/`.
- Eight routes: Tasks, Agents, Approvals, Audit, Costs, Missions, Fleet, Settings. Missions is the outcome-level timeline over a multi-day run: phase lanes, gate verdicts, envelope burn, and evidence links, each element tied to the receipt it was derived from. See [Screens](screens.md).
- Reads from `GET /api/v1/*` and the central SSE stream `GET /api/v1/events`. Uses TanStack Query 5 for caching; SSE events trigger invalidate-and-refetch (`invalidateQueries` on the affected query keys) rather than writing payloads into the cache directly.
- Known limitation: the SSE client uses the browser-native `EventSource`, which cannot send the `Authorization` header, so with auth enabled the event stream currently returns 401 and live invalidation does not run ([#3563](https://github.com/sipyourdrink-ltd/bernstein/issues/3563)). Panels still update via their interval polling (10-60 s).
- Auth via `Authorization: Bearer ${localStorage.bernstein_token}`. Token comes from `BERNSTEIN_AUTH_TOKEN` (auto-minted only on a loopback serve with auto-open - see quickstart above).

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
