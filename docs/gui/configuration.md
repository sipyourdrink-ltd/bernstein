---
title: Configuration
description: Environment variables, theme selection, and fleet mode toggle for the Bernstein GUI.
tags:
  - gui
  - configuration
---

# Configuration

## Environment variables

| Variable                    | Default     | Effect                                                  |
|-----------------------------|-------------|---------------------------------------------------------|
| `BERNSTEIN_AUTH_TOKEN`      | auto-gen    | Bearer token for `/api/v1/*`. SPA reads from `localStorage.bernstein_token`. If unset, the server generates one per session and logs it to stderr. |
| `BERNSTEIN_AUTH_DISABLED`   | unset       | Set to `1` / `true` / `yes` / `on` to bypass auth. Logs a loud warning. **Never on a network-reachable host.** |
| `BERNSTEIN_HOOK_SECRET`     | unset       | HMAC secret for hook endpoints. Falls back to `BERNSTEIN_AUTH_TOKEN` if unset. |
| `BERNSTEIN_MOCK_IDLE`       | unset       | When `1`, mock adapter sleeps each spawned agent. Set automatically by `bernstein run --idle`. |
| `BERNSTEIN_MOCK_IDLE_MIN_S` | `15`        | Lower bound of per-spawn idle sleep, seconds.           |
| `BERNSTEIN_MOCK_IDLE_MAX_S` | `120`       | Upper bound of per-spawn idle sleep, seconds.           |

Auth source: `src/bernstein/core/security/auth_middleware.py`. Idle source: `src/bernstein/cli/run_bootstrap.py` and `src/bernstein/adapters/mock.py`.

## Token plumbing

```text
operator shell  ──BERNSTEIN_AUTH_TOKEN──▶  bernstein gui serve  ──Bearer token──▶  /api/v1/*
                                                       │
                                                       └── stderr: "Auto-generated BERNSTEIN_AUTH_TOKEN ..."
operator browser  ──localStorage.bernstein_token──▶  Authorization: Bearer ...
```

Set the browser side once per machine:

```js
localStorage.setItem("bernstein_token", "<value of BERNSTEIN_AUTH_TOKEN>")
```

## Dashboard tokens

The dashboard requires a credential: on a loopback bind an operator token is issued and printed at startup; a non-loopback bind refuses to start until one is configured. Issue read-only (`viewer`) or read-write (`operator`) tokens with `bernstein auth dashboard-token issue --principal <name> --scope viewer`; every grant and write authorization is a signed governance record (`bernstein governance verify dashboard-auth`).

## Theme

- The SPA uses Tailwind 3 with shadcn/ui CSS variables. Light + dark are toggled by the `.dark` class on `<html>`.
- Tokens are defined in `web/src/index.css`.
- The theme respects `prefers-color-scheme` on first load and persists the operator's explicit choice via `localStorage`.
- `prefers-reduced-motion: reduce` disables non-essential transitions (motion spec in `web/src/lib/motion.ts`).

## Fleet mode toggle

- The topbar contains a `Single / Fleet` segmented control (defined in `web/src/components/AppShell.tsx`).
- **Single** - current process only. Reads `/api/v1/*` from one server.
- **Fleet** - multi-project view, backed by the fleet dashboard subsystem (`src/bernstein/core/fleet/`). Requires a fleet-enabled server.
- The toggle is a UI affordance only; it does not change auth or routing on its own. Configure the fleet endpoint upstream - see [Fleet (multi-project)](../operations/fleet.md).

## Build chip and `/health/deps`

The footer build chip reads `GET /api/v1/gui-meta` for `version` / `commit` / `build_time`, and `GET /health/deps` for the green/red status dot. Source: `src/bernstein/gui/__init__.py` (`gui_meta` route).
