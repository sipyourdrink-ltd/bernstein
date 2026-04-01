# Bernstein Configuration Reference

Bernstein configuration comes from three places:

1. `bernstein.yaml` (project seed/run config)
2. `.sdd/config.yaml` (workspace runtime defaults, created by `bernstein init`)
3. Environment variables (`BERNSTEIN_*`)

This document focuses on practical settings contributors actually use today.

---

## 1) Project config: `bernstein.yaml`

`bernstein.yaml` is the main run-time input. Typical keys include:

- `goal`
- `tasks`
- `workspace`
- `role_model_policy`
- `storage`
- `notify` (webhook/email/desktop notification settings)
- `network` (IP allowlist)

Minimal example:

```yaml
goal: "Implement API auth and integration tests"

tasks:
  - title: "Add auth middleware"
    role: backend
    priority: 1
    scope: medium
    complexity: medium

role_model_policy:
  backend:
    provider: claude_standard
    model: sonnet
    effort: high

storage:
  backend: memory
```

---

## 2) Workspace runtime defaults: `.sdd/config.yaml`

Created by:

```bash
bernstein init
```

Typical defaults:

- server port
- max workers/agents
- default model/effort

This file is local runtime state; `bernstein.yaml` remains the portable project config.

---

## 3) Environment variables

Environment variables are useful in CI and automation. Common variables:

| Variable | Purpose |
|---|---|
| `BERNSTEIN_SERVER_HOST` | Server bind address |
| `BERNSTEIN_SERVER_PORT` | Server port (default runtime is `8052`) |
| `BERNSTEIN_STORAGE_BACKEND` | Storage backend (`memory`, `postgres`, `redis`) |
| `BERNSTEIN_DATABASE_URL` | PostgreSQL DSN for `postgres`/`redis` backends |
| `BERNSTEIN_REDIS_URL` | Redis URL for distributed locking backend |
| `BERNSTEIN_SKIP_GATES` | Skip selected quality gates |
| `BERNSTEIN_SKIP_GATE_REASON` | Audit reason when gates are skipped |
| `BERNSTEIN_WORKFLOW` | Workflow mode override |
| `BERNSTEIN_ROUTING` | Routing policy override |
| `BERNSTEIN_COMPLIANCE` | Compliance preset override |
| `BERNSTEIN_QUIET` | Quiet mode (reduced terminal output) |
| `BERNSTEIN_AUDIT` | Enable extra audit behavior in run flow |

---

## Storage backends

Bernstein supports:

- `memory` (default, JSONL persistence)
- `postgres`
- `redis` (Postgres + Redis locking topology)

`bernstein.yaml` example:

```yaml
storage:
  backend: redis
  database_url: postgresql://user:pass@localhost/bernstein
  redis_url: redis://localhost:6379
```

You can validate effective connectivity with:

```bash
bernstein doctor
```

---

## Telemetry and metrics

- Prometheus metrics are exposed via `GET /metrics` on the server.
- OTLP endpoint wiring exists via telemetry configuration in the core config model.

Treat telemetry as configurable: enabled only when endpoint/settings are provided.

---

## Notifications

Seed-level notification settings support:

- webhook notifications
- SMTP email notifications
- optional desktop notifications

These are consumed by the notification manager and task lifecycle hooks.

---

## Network and safety controls

Configuration surface includes:

- IP allowlist (`network.allowed_ips`)
- role/model routing policy
- quality-gate controls
- audit controls

For security-sensitive deployments, prefer explicit config in `bernstein.yaml` over implicit defaults.

---

## Runtime bridges

Bernstein's production bridge configuration lives in `bernstein.yaml`.
The supported runtime bridge surface is:

```yaml
bridges:
  openclaw:
    enabled: true
    url: wss://gateway.openclaw.ai/ws
    api_key: ${OPENCLAW_API_KEY}
    agent_id: bernstein-shared-workspace
    workspace_mode: shared_workspace
    fallback_to_local: true
    connect_timeout_s: 10
    request_timeout_s: 900
    session_prefix: bernstein
    max_log_bytes: 262144
    model_override: null
```

`bridges.openclaw` is opt-in and intended for `shared_workspace` deployments
only. Bernstein remains the scheduler, verification owner, and merge owner;
OpenClaw executes against the same repository or shared filesystem that
Bernstein can inspect locally.

Field reference:

| Field | Meaning |
|---|---|
| `enabled` | Turn the bridge on for orchestration runs |
| `url` | OpenClaw Gateway WebSocket URL (`ws://` or `wss://`) |
| `api_key` | Gateway API key; `${VAR}` environment substitution is supported |
| `agent_id` | OpenClaw agent identifier to target |
| `workspace_mode` | Must be `shared_workspace` in the current implementation |
| `fallback_to_local` | Allow local CLI fallback only if bridge spawn fails before remote acceptance |
| `connect_timeout_s` | Gateway connect/auth timeout |
| `request_timeout_s` | Per-run wait timeout used by the bridge client |
| `session_prefix` | Prefix for Bernstein-owned remote session keys |
| `max_log_bytes` | Maximum log bytes returned by bridge log reads |
| `model_override` | Optional model pin forwarded to the bridge request |

### Notes on older bridge docs

Older notes that mention `.bernstein/config.toml`, bridge-specific `extra`
fields, or OpenClaw `/v1/sandboxes` REST semantics are obsolete. The repo-truth
production config surface is `bernstein.yaml` + `.sdd/config.yaml` +
`BERNSTEIN_*` environment variables, and the OpenClaw runtime path is the
Gateway WebSocket bridge implemented in Bernstein.
