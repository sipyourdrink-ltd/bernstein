---
title: Troubleshooting
description: Common GUI failure modes and their fixes.
tags:
  - gui
  - troubleshooting
---

# Troubleshooting

## `ModuleNotFoundError: sse_starlette` or `qrcode`

There is no runtime extras gate: `bernstein gui serve` runs on a plain install because `sse-starlette` arrives transitively via core deps and `fastapi` / `uvicorn` are already required (`src/bernstein/gui/cli.py` module docstring). You only need the `[gui]` extra to pin `qrcode` for `bernstein gui qr` / `--tunnel`.

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
