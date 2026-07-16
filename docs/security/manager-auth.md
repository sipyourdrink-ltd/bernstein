# Manager → task server authentication

How the spawned manager agent authenticates back to the local Bernstein task
server to create child worker tasks.

This page exists because the auth handshake between the manager subprocess and
the in-process task server is the single most common silent-stall failure in
multi-task plans (see issue #1261). Operators see "agent alive, no children
created, watchdog kills the server after 60 s" - the underlying cause is
almost always a missing or stripped bearer token on the manager's `POST
/tasks` call.

---

## TL;DR

| What | Where | Lifetime |
|------|-------|----------|
| Per-session bearer (zero-trust) | `.sdd/runtime/agent_tokens/{session_id}.token` (mode `0600`) | 4 h (task-scoped) |
| Legacy fallback bearer | `BERNSTEIN_AUTH_TOKEN` env var | Process lifetime |
| JWT signing secret | `.sdd/auth/jwt_secret` | Until manually rotated |
| Auth opt-out | `BERNSTEIN_AUTH_DISABLED=1` | Process lifetime - logs loud WARN |

Auth is **ENABLED by default**. A spawned manager that does not present a
valid token to `POST /tasks` receives HTTP 401 and produces no children.

---

## Overview

The task server is a local FastAPI process (default `http://127.0.0.1:8052`)
that owns the durable task store. The manager agent runs in its own git
worktree as a child subprocess of the orchestrator. When the manager
decomposes a goal into worker subtasks it must `POST /tasks` against the
server to register them - that request goes through the same auth
middleware as any other write call (`SSOAuthMiddleware` in
`src/bernstein/core/security/auth_middleware.py`).

Three credential paths exist; the manager may use whichever the orchestrator
provisions:

1. **Per-session zero-trust JWT** (default). Issued at spawn time, written to
   a `0600` file inside `.sdd/runtime/agent_tokens/`, scoped to the manager's
   task IDs.
2. **Legacy static bearer**. Set via `BERNSTEIN_AUTH_TOKEN`; propagated to
   the agent subprocess through the env isolation allowlist.
3. **Disabled** (`BERNSTEIN_AUTH_DISABLED=1`). Every request passes through;
   the middleware logs a one-shot SECURITY warning on startup.

---

## Components

```
┌───────────────┐  spawn (subprocess)     ┌──────────────────────────────┐
│ Orchestrator  ├────────────────────────►│ Manager agent (worktree)     │
│ (CLI process) │                         │ session_id=manager-xxxxxxxx  │
└──────┬────────┘                         └──────────────┬───────────────┘
       │                                                 │
       │ create JWT, write to                            │ read token file,
       │ .sdd/runtime/agent_tokens/{sid}.token           │ add Bearer header
       │                                                 │
       │ build_filtered_env() →                          ▼
       │   BERNSTEIN_AUTH_TOKEN if set            ┌──────────────────────┐
       │   BERNSTEIN_HOOK_SECRET                  │ POST /tasks          │
       │                                          │ Authorization: ...   │
       ▼                                          └──────────┬───────────┘
┌────────────────────────────────────────────────────────────▼──────────┐
│ Task server (FastAPI on 127.0.0.1:8052)                               │
│   SSOAuthMiddleware → AuthService.validate_token / legacy compare     │
└───────────────────────────────────────────────────────────────────────┘
```

Source pointers (read these if you need to debug from code):

| File | What it does |
|------|--------------|
| `src/bernstein/core/agents/spawner_core.py` (`_issue_agent_token`, `_render_auth_section`) | Mints the per-session JWT, writes it `0600`, injects path into the manager prompt. |
| `src/bernstein/adapters/env_isolation.py` (`_BASE_ALLOWLIST`) | Allowlist for cross-process env inheritance. Contains `BERNSTEIN_AUTH_TOKEN`, `BERNSTEIN_HOOK_SECRET`, `BERNSTEIN_AUTH_DISABLED`. |
| `src/bernstein/core/security/auth.py` (`AuthService`, `create_jwt`, `verify_jwt`) | JWT mint/verify (HS256), legacy bearer constant-time compare. |
| `src/bernstein/core/security/auth_middleware.py` (`SSOAuthMiddleware`, `auth_disabled_via_opt_out`) | Server-side gate. Implements the three-strategy chain (SSO JWT → agent JWT → legacy bearer). |
| `src/bernstein/core/server/server_app.py` (`create_app`) | Resolves `BERNSTEIN_AUTH_TOKEN` at boot, wires `legacy_auth_token` and `AgentIdentityStore` into middleware. |
| `src/bernstein/core/routes/auth.py` | `/auth/providers`, `/auth/cli/device`, `/auth/cli/token` (device-flow for human CLI logins - not used by spawned agents). |

---

## Where the token lives at runtime

### Per-session JWT (default path)

```
.sdd/
└── runtime/
    └── agent_tokens/
        └── manager-e0838d38.token          # mode 0600, contents = raw JWT
```

- **Created**: at agent spawn, inside `AgentSpawner._issue_agent_token`.
- **Format**: HS256 JWT signed with the secret from `.sdd/auth/jwt_secret`
  (auto-generated on first run; persisted across restarts).
- **Claims**: `sub=session_id`, `role`, `task_ids=[…]`, `iat`, `exp`, `jti`.
- **Expiry**: 4 h for task-scoped tokens (default 14400 s); 24 h
  for unrestricted (manager / orchestrator) tokens.
- **Revocation**: deleted from disk and revoked in `AgentIdentityStore` when
  the agent is reaped.
- **How the agent finds it**: the absolute path is interpolated into the
  manager's prompt via `_render_auth_section` (see
  `spawner_core.py:200`). The agent is instructed to read the file and add
  `Authorization: Bearer $(cat <path>)` to every task server request.

### Legacy static bearer

- **Env var**: `BERNSTEIN_AUTH_TOKEN`.
- **Read by server at**: `create_app()` in `server_app.py` (line ~909).
- **Read by agent**: present in `build_filtered_env()`'s `_BASE_ALLOWLIST`
  (`env_isolation.py:121`) so subprocesses inherit it automatically.
- **When used**: backwards compatibility for deployments that pre-date the
  per-session JWT flow.

### JWT signing secret

```
.sdd/
└── auth/
    └── jwt_secret                          # mode 0600, base64url 32-byte secret
```

Rotating this file invalidates **all** outstanding agent tokens. Restart the
orchestrator after rotation so the in-memory `AuthService` re-reads it.

---

## What can go wrong (and how to fix it)

### Symptom 1 - Manager spawned with stripped env → silent 401s → watchdog kill

This is the canonical #1261 failure.

**Indicators:**

- `.sdd/runtime/hooks/manager-*.jsonl` shows the manager probing
  `/tmp/openapi.json` and looking for `*.token` files on disk.
- `.sdd/runtime/server.log` shows 200 OKs to hook posts but **no successful
  `POST /tasks` request** from the manager session.
- Orchestrator log ends with `Server unresponsive for 60s - killing for
  restart`.
- No child tasks created, no files written for the actual goal.

**Cause:** Either the per-session token file was not generated (look for
`Issued zero-trust token for session manager-*` in `agent.log`), or the
token file path never reached the manager (it should be embedded in the
prompt - grep the prompt log for `Task Server Authentication`).

**Fix sequence:**

1. Confirm the token file exists:
   `ls -la .sdd/runtime/agent_tokens/manager-*.token`
   - should be `-rw-------` and non-empty.
2. Confirm the manager prompt contains the auth section:
   `grep -A4 "Task Server Authentication" .sdd/runtime/hooks/manager-*.jsonl`.
3. As a temporary workaround, set `BERNSTEIN_AUTH_TOKEN` before
   `bernstein run` so the legacy fallback path activates - it is inherited
   through the env allowlist regardless of whether the per-session token
   reaches the agent prompt.

### Symptom 2 - Auth accidentally left disabled

`BERNSTEIN_AUTH_DISABLED=1` (or `auth.enabled: false` in `bernstein.yaml`)
makes the middleware accept every request, including from any unrelated
process on `127.0.0.1`. The orchestrator emits one SECURITY WARN at startup
and never repeats it.

**Indicators:**

- `bernstein status` shows manager creating children but you don't remember
  configuring auth.
- `.sdd/runtime/orchestrator.log` line on startup:
  `SECURITY: Bernstein auth is DISABLED - every request is accepted without a Bearer token`.

**Fix:** `unset BERNSTEIN_AUTH_DISABLED` (or remove from `.env`), drop
`auth.enabled: false` from `bernstein.yaml`, restart.

### Symptom 3 - Token rotation between manager spawn and POST

If `.sdd/auth/jwt_secret` is rotated (or `.sdd/runtime/agent_tokens/` is
wiped) while a manager is mid-flight, that manager's cached token still
points at the old file path - `cat` returns empty / file-not-found, and the
resulting `Bearer ` header is rejected with 401.

**Indicators:** mid-run `401 Unauthorized` on `POST /tasks` after the
manager had previously succeeded.

**Fix:** never rotate `jwt_secret` or clear `agent_tokens/` while a run is
in flight. Use `bernstein stop` first.

---

## `bernstein doctor` checks

Run `bernstein doctor` for a top-level diagnostic. The relevant checks for
this flow are:

| Check | What it verifies |
|-------|------------------|
| Adapter auth (claude / codex / gemini) | The agent **provider** credentials. Does NOT verify Bernstein's internal task-server auth - these are different layers. |
| `.sdd/auth/jwt_secret` present | Per-session JWT issuance can succeed. |
| `.sdd workspace` | `.sdd/runtime/`, `.sdd/auth/` exist with correct modes. |

`bernstein doctor` does **not** yet have a dedicated check for the
manager-to-server bearer flow. Operators investigating #1261-class
failures should run the diagnostic commands below by hand.

---

## Diagnostic commands

Run these from the project root while a stuck `bernstein run` is in
progress. Each command prints the minimum signal needed to localise the
failure.

### 1. Is the per-session token actually on disk?

```bash
ls -la .sdd/runtime/agent_tokens/
```

Expected - at least one `manager-*.token`, mode `0600`, non-zero size:

```
-rw-------  1 you  staff  237 May 16 14:32 manager-e0838d38.token
```

If empty or absent: the spawner failed to mint a token. Check
`agent.log` for `Zero-trust token issuance failed for manager-…`.

### 2. Does the server accept the token?

```bash
TOKEN=$(cat .sdd/runtime/agent_tokens/manager-*.token | head -n 1)
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8052/tasks
```

Expected: `HTTP 200`. Anything else (especially `HTTP 401`) means the token
is rejected - re-check the JWT secret hasn't been rotated underneath.

### 3. Is auth even enabled?

```bash
curl -s http://127.0.0.1:8052/auth/providers | jq
```

Example healthy response:

```json
{
  "oidc_enabled": false,
  "saml_enabled": false,
  "legacy_token_enabled": true,
  "device_flow_enabled": false
}
```

If `legacy_token_enabled: false` AND no SSO providers AND
`BERNSTEIN_AUTH_DISABLED` is unset - the server boot did not pick up any
auth backend. Check `BERNSTEIN_AUTH_TOKEN` is exported in the shell that
launched `bernstein run`.

### 4. Did the manager prompt receive the token path?

```bash
grep -A6 "Task Server Authentication" .sdd/runtime/hooks/manager-*.jsonl | head -20
```

Expected - a block like:

```
## Task Server Authentication
Your agent token is stored at (do NOT print or log its contents):
.sdd/runtime/agent_tokens/manager-e0838d38.token
Include this header in **all** task server requests:
-H "Authorization: Bearer $(cat .sdd/runtime/agent_tokens/manager-e0838d38.token)"
```

If this block is missing the manager never learned the token path and will
spend its tokens probing the OpenAPI spec instead of POSTing tasks.

### 5. Did the manager actually try to authenticate?

```bash
grep -E "POST /tasks|401" .sdd/runtime/server.log | tail -20
```

A healthy run shows `POST /tasks 200`. A #1261-style failure shows either
no `POST /tasks` at all, or `POST /tasks 401`.

### 6. What does the manager subprocess have in its environment?

```bash
PID=$(cat .sdd/runtime/pids/manager-*.pid 2>/dev/null | head -n 1)
ps -p "$PID" -o pid,command
# On Linux:
tr '\0' '\n' < /proc/"$PID"/environ | grep -E "BERNSTEIN_"
# On macOS (requires sudo): ps eww -p "$PID"
```

Expected for legacy-bearer mode: `BERNSTEIN_AUTH_TOKEN=…` is present.
Expected for per-session JWT mode: `BERNSTEIN_AUTH_TOKEN` may be absent -
the agent is meant to read the `.token` file path from its prompt.

---

## Related issues

- **#1261** - *Manager agent cannot authenticate to task server to spawn
  child workers (multi-task plans hang).* External POC evaluator reported
  the manager spent its turn budget probing `/tmp/openapi.json` and
  `find … -name "*.token"` instead of constructing an authenticated
  request. This page exists because that issue explicitly asked the auth
  mechanism be exposed more prominently. The underlying bug (the internal
  `ManagerAgent.plan` HTTP client in
  `src/bernstein/core/orchestration/manager.py` does not yet attach a
  Bearer header when posting subtasks) is tracked separately.

## Related pages

- [Security hardening](security-hardening.md) - production lockdown
  checklist.
- [Credential scoping (default-on)](credential-scoping.md) - per-agent
  credential allowlist, complements the env-inheritance rules used here.
- [Capability matrix](capability-matrix.md) - which roles hold which
  permissions in the RBAC model the auth middleware enforces.
