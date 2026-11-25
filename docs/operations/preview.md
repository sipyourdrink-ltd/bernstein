# Preview

**Audience:** developers and PMs who want to view an agent's running output
(web app, dashboard, Storybook) before merge - without provisioning shared
staging.

**What:** `bernstein preview` boots a sandboxed dev server inside the
an isolated container with a read-only view of the selected workspace, opens a public
tunnel through the existing `bernstein tunnel` wrapper, mints a short-lived
auth credential, and prints a single shareable URL.

**Why:** Reviewing an agent's diff in your editor tells you nothing about
whether the rendered UI actually works. A preview link lets a non-technical
stakeholder click a URL and see the change live, scoped to one task and
auto-expiring within hours. See
`src/bernstein/cli/commands/preview_cmd.py:1-15`.

---

## What `preview start` does, end-to-end

Source: `src/bernstein/core/preview/manager.py:1-21`. One run-through:

1. **Discover** a runnable command -
   `src/bernstein/core/preview/command_discovery.py:1-18`. Precedence:
    1. `package.json` -> `scripts.dev`, then `scripts.start`.
    2. `Procfile` - first `web:` line, otherwise the first process.
    3. `.tool-versions` - surfaces runtime hint only.
    4. `bernstein.yaml::preview.command`.
2. **Approve the command** with `--command` or inspect the discovered command
   with `--list-commands` before passing `--approve-discovered`.
3. **Start an isolated dev server** using Docker or Podman and the local
   image named by `BERNSTEIN_PREVIEW_IMAGE`. The launcher passes argv directly
   and publishes `BERNSTEIN_PREVIEW_PORT` (default 3000) only on host loopback.
4. **Probe** `localhost:<port>` over TCP for up to 30 s before opening the
   tunnel (`src/bernstein/core/preview/__init__.py:79-95`).
5. **Tunnel** through `TunnelBridge` - primary provider, then
   `cloudflared` fallback when `--provider auto` and the primary binary is
   missing on PATH (`src/bernstein/core/preview/tunnel_bridge.py:70-119`).
6. **Mint credentials** via `PreviewTokenIssuer` - short-lived JWT, basic
   auth, or none. Credentials bake into the printed URL
   (`src/bernstein/core/preview/token_issuer.py:32-77`).
7. **Persist state** to `.sdd/runtime/preview/state.json` and append an
   HMAC-chained audit record. On any failure the manager rolls back: kill
   the dev-server, destroy the tunnel, drop the state row.

State file shape - `PreviewState`
(`src/bernstein/core/preview/manager.py:131-189`):

```json
{
  "previews": [
    {
      "preview_id": "p-abc12345",
      "command": "pnpm dev",
      "cwd": "/repo/.sdd/worktrees/task-42",
      "port": 5173,
      "sandbox_backend": "docker",
      "sandbox_session_id": "bernstein-preview-0123456789abcdef",
      "container_name": "bernstein-preview-0123456789abcdef",
      "tunnel_provider": "cloudflared",
      "tunnel_name": "...",
      "public_url": "https://abc.trycloudflare.com",
      "share_url": "https://abc.trycloudflare.com/?token=eyJ...",
      "auth_mode": "token",
      "expires_at_epoch": 1714857600,
      "process_pid": 0,
      "created_at_epoch": 1714843200
    }
  ]
}
```

---

## `bernstein preview` group

Source: `src/bernstein/cli/commands/preview_cmd.py:46-232`.

### `preview start`

```console
$ bernstein preview start [--cwd PATH] [--command "pnpm dev"]
                          [--provider auto|cloudflared|ngrok|bore|tailscale]
                          [--auth basic|token|none]
                          [--expire 30m|4h|1d]
                          [--list-commands | --approve-discovered]
                          [--no-clipboard]
```

Defaults: `--cwd` = most recent worktree under `.sdd/worktrees/`,
`--provider` = `auto` -> `cloudflared`, `--auth` = `token`,
`--expire` = `4h`. The URL is auto-copied to the clipboard (best-effort)
unless `--no-clipboard`.

`--list-commands` prints discovered candidates without starting a process.
Set `BERNSTEIN_PREVIEW_IMAGE` to an installed application image with its
runtime and dependencies. Configure the dev server to listen on its container
interface and the selected port. The workspace is read-only. Use `/tmp` for
temporary application state.

Output:

```
Started preview p-abc12345 (cloudflared -> localhost:5173)
URL: https://abc.trycloudflare.com/?token=eyJ...
auth=token  sandbox=docker/bernstein-preview-0123456789abcdef  expires_epoch=1714857600
URL copied to clipboard.
```

### `preview list`

```console
$ bernstein preview list [--json]
```

Prints a fixed-width table (or JSON array with `--json`) of every active
preview row from `state.json` (`cli/commands/preview_cmd.py:165-186`).

### `preview status <preview_id>`

```console
$ bernstein preview status p-abc12345 [--json]
```

Prints the full `PreviewState` payload for one preview
(`cli/commands/preview_cmd.py:194-208`).

### `preview stop`

```console
$ bernstein preview stop <preview_id>
$ bernstein preview stop --all
```

Cleanly tears down: kill dev-server PID, destroy tunnel, drop the row from
`state.json` (`cli/commands/preview_cmd.py:216-232`).

---

## Tunnel providers

The bridge delegates to `TunnelRegistry` shared with `bernstein tunnel`.
Built-in driver kinds are auto-registered via `register_default_drivers`
(`src/bernstein/core/preview/tunnel_bridge.py:20`); the wheel ships
`cloudflared`, `ngrok`, `bore`, `tailscale` driver bindings in
`core/tunnels/drivers/`. Any provider whose CLI binary is on PATH works.

`--provider auto` first asks the registry to pick the best available.
If no provider qualifies, the bridge re-tries explicitly with
`cloudflared` because the ticket pins it as the documented fallback
(`src/bernstein/core/preview/tunnel_bridge.py:96-115`). When even
`cloudflared` is missing the bridge raises `TunnelBridgeError` with the
provider's installation hint embedded.

---

## Security considerations

**The URL is the credential.** Anyone with the share URL hits the dev
server. Three auth modes layer on top:

- `--auth token` (default) - JWT in `?token=...`. Validity = `--expire`.
  Issued via `PreviewTokenIssuer` over the security layer's
  `JWTManager`, so revocation works through the existing
  `/auth/{revoke,validate}` endpoints
  (`src/bernstein/core/preview/token_issuer.py:1-17`).
- `--auth basic` - random strong password baked into a
  `https://user:pass@host` URL.
- `--auth none` - bare tunnel URL. Acceptable only when the dev server
  itself enforces auth (very rare). Default-off for that reason.

Other guarantees:

- Every state-changing transition (start/stop) appends an HMAC-chained
  entry to `.sdd/audit` so a misuse trail survives even after the
  preview row is deleted
  (`src/bernstein/core/preview/manager.py:17-19`).
- The preview runtime mounts the selected workspace read-only and publishes its
  assigned application port on host loopback. It passes no host environment
  variables to the application. The operator selects the application image.
- Tokens expire by default at 4 h. Increase only when you have a reason;
  reviewers leaking a 24 h URL on Slack is a real failure mode.
- The `share_url` field in `state.json` includes the token. Treat
  `.sdd/runtime/preview/` as confidential; the `_state_to_payload` helper
  redacts nothing (`cli/commands/preview_cmd.py:285-306`).
- Default expire bound: 4 h
  (`src/bernstein/core/preview/manager.py:66`). Operators set a stricter
  ceiling via the `--expire` argument; there is no global cap today.

---

## Configuration

The preview uses these settings and state files:

| Where                                 | Knob                          | Effect                                              |
|---------------------------------------|-------------------------------|-----------------------------------------------------|
| Operator environment | `BERNSTEIN_PREVIEW_IMAGE` | Installed Docker or Podman image containing the dev runtime |
| Operator environment | `BERNSTEIN_PREVIEW_PORT` | Container application port, default 3000 |
| `bernstein.yaml`                      | `preview.command`             | 4th-precedence dev-server command override          |
| `.sdd/runtime/preview/state.json`     | persisted on every start      | live registry consumed by `list`/`status`/`stop`    |
| `.sdd/audit/`                         | HMAC chain                    | audit log for every preview lifecycle transition    |

Tunnel state is **shared** with `bernstein tunnel`, so the bridge picks up
provider configuration that lives in `core/tunnels/registry.py`'s state
file. There is no preview-specific tunnel config today.

`PREVIEW_STATE_DIR` and `DEFAULT_AUDIT_DIR` are constants in
`src/bernstein/core/preview/manager.py:62-64`; tests can override via
the `PreviewStore` and `AuditLog` injection points.

---

## Observability

Three Prometheus counters (registered in
`src/bernstein/core/preview/metrics.py`):

- `preview_started_total{provider, auth_mode}` - every successful start.
- `preview_stopped_total{reason}` - every stop (operator, expiry, crash).
- `preview_link_issued_total{auth_mode}` - every credential mint.

Surface via the existing `/metrics` endpoint. There is no separate
dashboard today; see `docs/operations/observability-overview.md` for the
broader observability plan.

---

## Code pointers

- `src/bernstein/cli/commands/preview_cmd.py:46-232` - CLI surface
- `src/bernstein/core/preview/__init__.py` - public API exports
- `src/bernstein/core/preview/manager.py:1-21` - lifecycle rationale
- `src/bernstein/core/preview/manager.py:62-189` - `PreviewState` + `PreviewStore`
- `src/bernstein/core/preview/manager.py:298-...` - `PreviewManager` orchestration
- `src/bernstein/core/preview/command_discovery.py` - auto-discovery precedence
- `src/bernstein/core/preview/port_capture.py` - port detection regex + TCP probe
- `src/bernstein/core/preview/tunnel_bridge.py:38-119` - tunnel facade + cloudflared fallback
- `src/bernstein/core/preview/token_issuer.py` - JWT/basic/none credentials
- `src/bernstein/core/preview/metrics.py` - Prometheus counters
