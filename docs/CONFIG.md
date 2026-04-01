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

## Notes on older bridge docs

If you are looking for runtime bridge details (`.bernstein/config.toml`, bridge-specific `extra` fields), treat that as advanced/experimental integration material. The day-to-day production config surface is currently `bernstein.yaml` + `.sdd/config.yaml` + `BERNSTEIN_*` environment variables.
