# Bernstein Configuration Reference

Bernstein configuration comes from three places:

1. `bernstein.yaml` (project seed/run config)
2. `.sdd/config.yaml` (workspace runtime defaults, created by `bernstein init`)
3. Environment variables (`BERNSTEIN_*`)

This document focuses on practical settings contributors actually use today.

All magic numbers, timeouts, and thresholds are centralized in `src/bernstein/core/defaults.py` and can be overridden via the `tuning:` section in `bernstein.yaml` or via environment variables.

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
- `tuning` (override defaults from `core/defaults.py`)

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
    cli: qwen
    model: coder-model
    effort: high
  security:
    cli: gemini
    model: gemini-3.1-pro-preview
    effort: high

# Internal LLM for orchestrator scheduling decisions (plan decomposition,
# cost estimation, auto-decompose). Accepts ANY supported adapter name.
internal_llm_provider: gemini
internal_llm_model: gemini-3.1-pro-preview
```

### `internal_llm_provider` — Orchestrator scheduling model

The orchestrator uses a lightweight LLM for internal decisions: task decomposition, cost estimation, difficulty scoring, and plan optimization. This is **not** the agent — it's the scheduler's brain.

Any registered adapter CLI can serve as the internal LLM provider:

| Provider | Model example | Notes |
|----------|---------------|-------|
| `gemini` | `gemini-3.1-pro-preview` | Free tier, 1M context, strong reasoning |
| `qwen` | `coder-model` | Free via Qwen OAuth, good for coding tasks |
| `claude` | `claude-sonnet-4-6` | Strongest reasoning, requires Claude Code CLI |
| `codex` | `gpt-5.4-mini` | OpenAI models via Codex CLI |
| `ollama` | `deepseek-r1:70b` | Fully local, no API calls |
| `goose` | `claude-sonnet-4-6` | Block's Goose CLI |
| `aider` | `claude-sonnet-4-6` | Aider CLI (any provider backend) |
| `openrouter` | `nvidia/nemotron-3-super-120b-a12b` | API-based, requires `OPENROUTER_API_KEY` |

Set via `bernstein.yaml`:
```yaml
internal_llm_provider: gemini          # adapter name
internal_llm_model: gemini-3.1-pro-preview  # model passed to the CLI
```

Or via environment:
```bash
export BERNSTEIN_INTERNAL_LLM_PROVIDER=qwen
export BERNSTEIN_INTERNAL_LLM_MODEL=coder-model
```

### `role_model_policy` — Per-role agent configuration

Each role can use a different CLI adapter and model:

```yaml
role_model_policy:
  manager:
    cli: qwen           # which CLI agent to spawn
    model: coder-model   # model name passed to that CLI
    effort: max          # controls max_turns and budget
  backend:
    cli: gemini
    model: gemini-3.1-pro-preview
    effort: high
  docs:
    cli: claude
    model: sonnet
    effort: medium
```

When `cli: auto` is set at the top level, the orchestrator picks the best available adapter per role based on the `role_model_policy`. When a specific `cli:` is set per role, that adapter is used exclusively for that role.

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

## 4) Tunable defaults: `core/defaults.py`

All magic numbers live in `src/bernstein/core/defaults.py` as typed dataclasses. Override at runtime via the `tuning:` section in `bernstein.yaml`:

```yaml
tuning:
  orchestrator:
    tick_interval_s: 5.0
    drain_timeout_s: 120.0
    stale_claim_timeout_s: 1800.0
  spawn:
    spawn_backoff_base_s: 60.0
```

Key default groups:

| Group | Examples |
|-------|---------|
| `OrchestratorDefaults` | `tick_interval_s`, `drain_timeout_s`, `max_consecutive_failures`, `stale_claim_timeout_s` |
| `SpawnDefaults` | `spawn_backoff_base_s`, `spawn_backoff_max_s`, `max_spawn_failures` |
| `TaskDefaults` | Retry limits, deadline windows |
| `AgentDefaults` | Heartbeat intervals, max dead agents kept |

See `defaults.py` for the full list of parameters and their default values.

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

## Always-allow rules (`.bernstein/always_allow.yaml`)

Always-allow rules short-circuit approval prompts when a tool invocation matches a known-safe signature. For example, allowing `grep` on `src/*` paths while still asking or denying `grep` on `/etc`.

**Precedence:** Always-allow rules take **highest precedence** — a match overrides any ASK or DENY from other guardrails. `IMMUNE` and `SAFETY` decisions (e.g. secret detection, immune-path enforcement) are **never** overridden.

### Rule schema

```yaml
# .bernstein/always_allow.yaml
- id: safe-grep-src           # unique kebab-case ID
  tool: grep                  # tool name to match (case-insensitive)
  input_pattern: "src/.*"     # regex (if contains .* or ^ or $) or glob
  input_field: path           # which arg to match against (default: path)
  content_patterns:           # optional: ALL must be present in full_content
    - "--include=*.py"
    - "--recursive"
  description: "Recursive Python grep on src/ only"
```

Alternatively, embed under an `always_allow:` key in `.bernstein/rules.yaml`.

### Pattern syntax

| Pattern | Interpreted as |
|---------|----------------|
| `src/.*` | Regex (contains `.*`) |
| `^tests/` | Regex (anchored) |
| `tests/*` | Glob |

### `content_patterns`

When `content_patterns` is specified, **all** listed strings must appear in the full tool invocation content for the rule to fire. This enables narrower constraints, e.g. allowing `grep` only when both `--include=*.py` and the path are present.

### Precedence summary

```
IMMUNE / SAFETY   — never overridden (secrets, immune paths)
ALWAYS-ALLOW      — overrides ASK and DENY when a rule matches
DENY              — blocks by rule
ASK               — prompts user
ALLOW             — default pass-through
```

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
    request_timeout_s: 30.0
    session_prefix: bernstein-
    max_log_bytes: 1048576
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
