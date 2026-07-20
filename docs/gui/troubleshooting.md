---
title: Troubleshooting
description: Common GUI failure modes and their fixes.
tags:
  - gui
  - troubleshooting
---

# Troubleshooting

## `ModuleNotFoundError: sse_starlette` or `qrcode`

There is no runtime extras gate - `bernstein gui serve` runs on a plain install. See [Install the extras](install.md#install-the-extras) for why. You only need the `[gui]` extra to pin `qrcode` for `bernstein gui qr` / `--tunnel`.

**Fix.**

```bash
pip install 'bernstein[gui]'
```

If you installed editable from source, re-run with the extras:

```bash
pip install -e '.[gui]'
```

## "GUI static assets not found at …"

```text
RuntimeError: GUI static assets not found at /…/src/bernstein/gui/static.
Build them with: `cd web && npm install && npm run build`
```

**Cause.** The wheel was built without the React bundle, or you cloned the repo and tried to `bernstein gui serve` before building.

**Fix.**

```bash
cd web
npm install
npm run build      # writes to ../src/bernstein/gui/static/
```

The committed `static/` directory is what the wheel ships; rebuild whenever `web/src/` changes.

## Port collision on `:8052`

```text
[Errno 48] Address already in use
```

**Cause.** Another Bernstein server, FastAPI process, or unrelated service holds `:8052`.

**Fix.** Pass `--port`:

```bash
bernstein gui serve --port 8765
```

Or kill the holder:

```bash
lsof -i :8052           # macOS / Linux
kill <pid>
```

The launcher does **not** auto-fall back to a free port.

## White screen on `/ui/`

Symptoms: navigation succeeds, page renders blank, no obvious error in the terminal.

**Probable causes, in order:**

1. **Browser blocked the bundle.** Open DevTools → Console. A 401 / 403 on `/api/v1/gui-meta` means the bearer token is missing or wrong.

    ```js
    localStorage.setItem("bernstein_token", "<value of BERNSTEIN_AUTH_TOKEN>")
    location.reload()
    ```

2. **Static assets path mismatch.** The mount point is `/ui/` (trailing slash). `/ui` (no slash) returns the SPA, but if your reverse proxy strips the trailing slash from `/ui/assets/*` URLs, asset requests 404. Configure the proxy to preserve trailing slashes.

3. **Stale `localStorage`.** Old token from a prior session. Clear it:

    ```js
    localStorage.clear()
    ```

    Then re-set `bernstein_token`.

4. **Build artifact mismatch.** `bernstein gui serve --minimal` and watch DevTools Network. If `/api/v1/gui-meta` returns a build_time older than your last `npm run build`, you're serving a stale wheel - reinstall.

## Dashboard panels 401 on a local `gui serve`

Symptoms: the shell loads, but every data panel (Tasks, Agents, Costs, ...)
shows an auth error. DevTools Network shows `/api/v1/*` returning `401`.

**Cause.** The panels poll the authenticated general API (`/api/v1/agents`,
`/api/v1/tasks`, ...). That surface accepts the process `BERNSTEIN_AUTH_TOKEN`
bearer. (A `bernstein auth dashboard-token` scoped token only unlocks the
`/api/v1/dashboard/*` mirror - not these routes.) A bare local `serve` used to
open `/ui/` with no credential, so the browser had nothing to send.

**Fix (automatic on loopback).** On a loopback bind (`127.0.0.1` / `localhost` /
`::1`) with no `BERNSTEIN_AUTH_TOKEN` set, `serve` now mints an ephemeral
operator bearer, exports it for the API, and opens the browser pre-seeded with
the same token - the panels authenticate with zero manual steps. `serve` prints
`Dashboard ready at http://127.0.0.1:8052/ui/ (browser authenticated
automatically).` to confirm. Nothing to configure:

```bash
bernstein gui serve
```

To pin a fixed bearer instead (for example to re-pair a device or share the
same token across restarts), set it explicitly and `serve` reuses it verbatim:

```bash
BERNSTEIN_AUTH_TOKEN=<your-secret> bernstein gui serve
```

Either way the token rides the opened browser's URL fragment only (the SPA
scrubs it from the address bar after capture) - it never appears in the console
URL or the access log. Posture is unchanged: a non-loopback bind still refuses
to start without configured auth and is never auto-minted a bearer, and an
external, tokenless `/api/v1/*` request still returns `401`.

Manual fallback (any bundle): set the key the API reads and reload.

```js
localStorage.setItem("bernstein_token", "<value of BERNSTEIN_AUTH_TOKEN>")
location.reload()
```

For a dev-only open bind with no token at all, `BERNSTEIN_AUTH_DISABLED=1`
drops auth (never on a network-exposed host).

## Theme not switching

- The dark/light toggle flips the `.dark` class on `<html>`. Verify in DevTools Elements that `<html class="dark">` is present (or absent) when you click the toggle.
- If the class flips but colors stay light, `web/src/index.css` was not loaded - confirm `<link rel="stylesheet" …>` resolves to `/ui/assets/index-*.css`.
- If the page never honors `prefers-color-scheme`, check that the operator hasn't pinned a theme via `localStorage.theme` - clear it to fall back to system.
- Token definitions: `web/src/index.css`.

## Sidebar Approvals badge stuck at zero

- Badge count comes from `useQuery(['approvals','queue'])`. If the queue endpoint returns `[]` even when approvals exist, the SSE stream isn't hydrating the cache.
- Verify `GET /api/v1/events` is open in DevTools Network (look for `EventStream` type).
- If SSE is blocked by a corporate proxy that buffers responses, the live updates will not arrive - the page will only refresh on poll. Bypass the proxy or use `bernstein gui serve --dev` against a local dev session.

## Auth disabled but warning floods the log

`BERNSTEIN_AUTH_DISABLED=1` is intentionally noisy:

```text
SECURITY: Bernstein auth is DISABLED - every request is accepted without
a Bearer token (opt-out via BERNSTEIN_AUTH_DISABLED or auth.enabled=false).
Do NOT run this configuration on any network-exposed host.
```

The warning fires once per process. If you see it repeatedly, multiple Bernstein processes are running - `ps aux | grep bernstein` and reconcile.
