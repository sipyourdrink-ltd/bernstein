---
search:
  boost: 2
---

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
- `local_endpoints`
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
    model: gemini-pro
    effort: high

# Internal LLM for orchestrator scheduling decisions (plan decomposition,
# cost estimation, auto-decompose). Accepts ANY supported adapter name.
internal_llm_provider: gemini
internal_llm_model: gemini-pro
```

### `internal_llm_provider` - Orchestrator scheduling model

The orchestrator uses a lightweight LLM for internal decisions: task decomposition, cost estimation, difficulty scoring, and plan optimization. This is **not** the agent - it's the scheduler's brain.

Any registered adapter CLI can serve as the internal LLM provider:

| Provider | Model example | Notes |
|----------|---------------|-------|
| `gemini` | `gemini-pro` | Free tier, 1M context, strong reasoning |
| `qwen` | `coder-model` | Free via Qwen OAuth, good for coding tasks |
| `claude` | `claude-sonnet-4-6` | Strongest reasoning, requires Claude Code CLI |
| `codex` | `gpt-5-mini` | OpenAI models via Codex CLI |
| `ollama` | `deepseek-r1:70b` | Fully local, no API calls |
| `goose` | `claude-sonnet-4-6` | Block's Goose CLI |
| `aider` | `claude-sonnet-4-6` | Aider CLI (any provider backend) |
| `openrouter` | `nvidia/nemotron-3-super-120b-a12b` | API-based, requires `OPENROUTER_API_KEY` |

Set via `bernstein.yaml`:
```yaml
internal_llm_provider: gemini          # adapter name
internal_llm_model: gemini-pro  # model passed to the CLI
```

Or via environment:
```bash
export BERNSTEIN_INTERNAL_LLM_PROVIDER=qwen
export BERNSTEIN_INTERNAL_LLM_MODEL=coder-model
```

### `role_model_policy` - Per-role agent configuration

Each role can use a different CLI adapter and model:

```yaml
role_model_policy:
  manager:
    cli: qwen           # which CLI agent to spawn
    model: coder-model   # model name passed to that CLI
    effort: max          # controls max_turns and budget
  backend:
    cli: gemini
    model: gemini-pro
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
| `BERNSTEIN_BIND_HOST` | Server bind address (default `127.0.0.1`) |
| `BERNSTEIN_PORT` | Server port (default `8052`) |
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
| `BERNSTEIN_STALL_THRESHOLD_S` | Override the manager-stall deadline (see §4.2 below). Takes precedence over the `tuning.orchestrator.stalled_manager_threshold_s` yaml key. |
| `BERNSTEIN_MAX_TURNS` | Override the SDK turn ceiling for the openai_agents runner (see §4.1). Takes precedence over `tuning.agent.max_turns`. |
| `BERNSTEIN_JANITOR_FUZZY_PATHS` | Opt-in fuzzy matching for `path_exists` completion signals. **Default `0` (off)**: exact-path matching is unchanged - the check means exactly the path written in the plan. Set to `1` to enable a fuzzy fallback on a literal miss: a pattern derived from the basename (`**/<stem>*<suffix>`) so a manager-guessed path (e.g. `packages/db/test/foo.test.ts`) can match a worker's repo-idiomatic path (e.g. `packages/db/src/__tests__/foo.test.ts`). A fuzzy match logs a WARNING naming the matched path. Explicit glob syntax written in the criterion itself (`*`, `?`, `[`) is always honored, regardless of this flag. See [TROUBLESHOOTING.md #21](TROUBLESHOOTING.md#21-janitor-path_exists-fails-on-a-correctly-placed-file-acceptance-path-mismatch). |
| `BERNSTEIN_JANITOR_REOPEN_MAX` | Max janitor-reopen cycles per task before a janitor-FAILed task is permanently failed instead of reopened under the same id. **Default `2`.** A non-numeric value logs a warning and falls back to the default; negative values are clamped to `0` (fail immediately, never reopen). |
| `BERNSTEIN_QUIESCENCE_SETTLE_S` | Settle delay (seconds) the orchestrator waits after a tick reaches quiescence (all tasks terminal) before the re-check that decides whether to self-stop. **Default `2.0`.** Set `0` to skip the settle sleep entirely (used by tests to avoid a real per-tick delay). |
| `BERNSTEIN_RUN_COMMAND_DEDUPE_WINDOW_S` | Dedupe window (seconds) for the `run_command` builtin: an identical command (same hash) still in flight or finished within this window reuses the original's result instead of re-executing. **Default `2.0`.** Set `0` to disable the dedupe guard entirely. |
| `BERNSTEIN_OPENAI_AGENTS_TOOL_SOURCE` | Operator lever for the `openai_agents` runner's tool source. Set to `builtin` to make every spawn use the runner's workdir-sandboxed builtin tools (`read_file`/`write_file`/`list_dir`/`run_command`) even when the per-spawn `mcp_config` carries no `tool_source` key. An explicit `mcp_config["tool_source"]` value always wins over this env var. Unset by default. |

---

## 4) Tunable defaults: `core/defaults.py`

All magic numbers live in `src/bernstein/core/defaults.py` as typed dataclasses. Override at runtime via the `tuning:` section in `bernstein.yaml`:

```yaml
tuning:
  orchestrator:
    tick_interval_s: 5.0    # default 3.0 (ORCHESTRATOR.tick_interval_s in core/defaults.py)
    drain_timeout_s: 120.0
    stale_claim_timeout_s: 1800.0
    stalled_manager_threshold_s: 300.0
    max_agent_runtime_s: 3600.0
  spawn:
    spawn_backoff_base_s: 60.0
  agent:
    max_turns: 200
  slo:
    error_budget_min_failures: 10
```

Key default groups:

| Group | Examples |
|-------|---------|
| `OrchestratorDefaults` | `tick_interval_s`, `drain_timeout_s`, `max_consecutive_failures`, `stale_claim_timeout_s`, `stalled_manager_threshold_s`, `max_agent_runtime_s` |
| `SpawnDefaults` | `spawn_backoff_base_s`, `spawn_backoff_max_s`, `max_spawn_failures` |
| `TaskDefaults` | Retry limits, deadline windows |
| `AgentDefaults` | Heartbeat intervals, max dead agents kept, `max_turns` |
| `MemoryChainDefaults` | `default_scope` (`user`/`agent`/`run`/`app`), `retention_days` (reporting horizon only; the append-only chain is always retained in full) |
| `SLODefaults` | `error_budget_min_failures` |

### 4.1) Agent run-length limits (SDK turns, error budget, wall-clock)

Three previously-hardcoded ceilings on the `openai_agents` adapter path are
now tunable, with defaults unchanged so nothing breaks for existing users:

- **`max_turns`** (`AgentDefaults.max_turns`, default `30`) - forwarded to
  the OpenAI Agents SDK's `Runner.run_sync(..., max_turns=...)`. The runner
  forwards `max_turns=30` by default; set `tuning.agent.max_turns` to `None`
  to omit the kwarg and fall back to the SDK's own default (`10`).
  A task that legitimately needs more turns
  before `MaxTurnsExceeded` (large-repo investigation, multi-file
  refactors) can raise this via `tuning.agent.max_turns: 200` or the
  `BERNSTEIN_MAX_TURNS` env var (checked first). `BERNSTEIN_MAX_TURNS` must
  parse as a positive integer - `0`, a negative value, or a non-numeric
  string is rejected with a warning and falls through to
  `tuning.agent.max_turns`/the SDK default, rather than being forwarded to
  `Runner.run_sync` where it would fail every run on the first turn.
- **`error_budget_min_failures`** (`SLODefaults.error_budget_min_failures`,
  default `3`) - the floor `ErrorBudget.budget_total` never goes below,
  even when `total_tasks * (1 - slo_target)` rounds lower. Raise it via
  `tuning.slo.error_budget_min_failures` when a run's early failures are
  infra-death retries (rate limits, transient auth) that shouldn't trip
  `IncidentManager`'s auto-pause. A negative override is clamped to `0`
  rather than being allowed to suppress the SLO-target-derived budget.
- **`max_agent_runtime_s`** (`OrchestratorDefaults.max_agent_runtime_s`,
  default `1800`) - the starting wall-clock kill deadline for a spawned
  agent, now sourced through `OrchestratorConfig` via the same
  `tuning.orchestrator.*` mechanism as `stale_claim_timeout_s`. This is
  only the *starting* value - the agent lifecycle reaper already
  self-extends it by 600s per cycle up to a 5400s hard cap while the agent
  keeps heartbeating.

See `defaults.py` for the full list of parameters and their default values.

### 4.2) Manager-stall deadline

`core/orchestration/stalled_manager.py` aborts a run when the manager agent
has been alive for `stalled_manager_threshold_s` (default `170.0`) with zero
child tasks posted to the task server. On a larger codebase, or a goal whose
acceptance criteria demand root-cause investigation before decomposition, the
manager can legitimately need longer than that before its first `POST /tasks`
call. Raise the deadline via either:

```yaml
tuning:
  orchestrator:
    stalled_manager_threshold_s: 900.0
```

or the `BERNSTEIN_STALL_THRESHOLD_S` env var (checked first, so it overrides
the yaml value for a single run without editing `bernstein.yaml`). Precedence:
env var > yaml `tuning.orchestrator.stalled_manager_threshold_s` > the
`170.0` default. Each tier must resolve to a positive, finite number of
seconds - an unparseable, zero, negative, `nan`, or `inf` value at a given
tier is rejected with a warning and falls through to the next tier, rather
than silently disabling the watchdog (fires on turn one) or making it a
permanent no-op.

If the resolved threshold reaches or exceeds `AGENT.idle_log_age_threshold_s`
(`180.0` by default - a separate, currently-unwired idle-agent watchdog), a
warning is logged, since that watchdog would race the stalled-manager
diagnostic if it is ever wired into the tick loop. This check runs for the
env var, yaml, and default resolution paths alike, so raising the threshold
via `BERNSTEIN_STALL_THRESHOLD_S` - the primary way an operator raises it -
surfaces the warning too. The resolution (and this check) happens once per
orchestrator instance, not on every tick.

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

## Bootstrap and merge safety

### GitHub backlog auto-sync (opt-in, off by default)

At bootstrap, Bernstein can pull open GitHub issues into `.sdd/backlog/open/`
before task scoping. This is **opt-in and off by default** so it cannot
silently fill the backlog and displace a seeded goal.

Enable it in `bernstein.yaml`:

```yaml
github:
  sync_backlog: true   # default: false
```

Or override at runtime (wins over the config value):

```bash
BERNSTEIN_SYNC_GITHUB_BACKLOG=1   # truthy enables; falsy (0/false/off) disables
```

When disabled (the default), no issues are synced.

### Goal-vs-backlog precedence

When a run starts, the planning source is chosen in this order:

1. **Prior session** - resume and skip re-planning.
2. **Non-empty backlog** - run the backlog tasks.
3. **Seed goal** - inject the manager/worker task from `goal`.

So a **non-empty backlog takes precedence over the seed goal**. This is kept
for back-compat with backlog-driven runs, but Bernstein now prints a LOUD
warning when a seeded goal is skipped because the backlog is non-empty. To run
the goal instead:

- start from an empty backlog (clear `.sdd/backlog/open/`), or
- narrow the run with the `BERNSTEIN_TASK_FILTER` sentinel so no backlog task
  matches.

### Default-branch merge guard

Agent worktree work is merged into whatever branch is checked out at the repo
root. Bernstein **refuses to merge or push agent work onto the repository's
default (protected) branch** (`main`/`master`, resolved via `origin/HEAD` with
a `init.defaultBranch` fallback). The refusal is recorded to
`.sdd/runtime/refused_merges.jsonl` and the merge is reported as failed rather
than silently landing unreviewed commits on the trunk.

Check out a non-default branch before merging, or opt in explicitly:

```bash
BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH=1
```

---

## Always-allow rules (`.bernstein/always_allow.yaml`)

Always-allow rules short-circuit approval prompts when a tool invocation matches a known-safe signature. For example, allowing `grep` on `src/*` paths while still asking or denying `grep` on `/etc`.

**Precedence:** Always-allow rules take **highest precedence** - a match overrides any ASK or DENY from other guardrails. `IMMUNE` and `SAFETY` decisions (e.g. secret detection, immune-path enforcement) are **never** overridden.

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
IMMUNE / SAFETY   - never overridden (secrets, immune paths)
ALWAYS-ALLOW      - overrides ASK and DENY when a rule matches
DENY              - blocks by rule
ASK               - prompts user
ALLOW             - default pass-through
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

---

## `local_endpoints` - Local-model worker tier

Named OpenAI-compatible endpoint profiles for local runtimes (ollama, LM
Studio, MLX servers). Role entries reference a profile by name and inherit
its `base_url`/`model`/`api_key_env`:

```yaml
local_endpoints:
  workhorse:
    base_url: http://127.0.0.1:11434/v1
    model: qwen2.5-coder:7b-instruct-q4_K_M
    engine: ollama          # free-form label recorded in the certification receipt
    timeout: 120            # request timeout in seconds
    # api_key_env: LOCAL_LLM_API_KEY  # NAME of an env var, never a literal key

role_model_policy:
  linter:      { endpoint: workhorse }
  test_writer: { endpoint: workhorse }
```

Low-stakes roles (`linter`, `test_writer`, `triage`, `doc_sweeper`) run on a
profile without further ceremony. Every other role is merge-critical and
requires a signed certification receipt produced by
`bernstein doctor --endpoint <url>`; an uncertified profile assigned to a
gated role fails config validation with the exact command to run. See
[Local endpoints](../reference/local-endpoints.md) for the conformance
subset, role tiers, and verified configurations.

## Provider availability (`provider_availability`)

Per-role provider fallback chains with conformance floors. Before dispatch
the scheduler health-probes the declared chain (probe results cached for
`probe_ttl_minutes`) and picks the first healthy element; the decision is
recorded as a routing receipt in the audit chain.

```yaml
provider_availability:
  probe_ttl_minutes: 5
  probes_enabled: true      # false for offline runs
  roles:
    developer:
      conformance_floor: advanced
      chain:
        - {adapter: claude, model: opus, conformance: expert}
        - {adapter: codex, model: gpt-5.2, conformance: advanced}
```

A chain element whose `conformance` is below the role's
`conformance_floor` fails config validation. Validate declared chains with
`bernstein doctor --failover-drill`. Full guide:
[Provider availability & failover](provider-availability.md).

## Fleet config plane (#2550)

Three named-configuration primitives share one discipline: every write,
rotation, and activation is an audit-chain event, every claim-time read is
pinned into task lineage, and history, divergence, and replay all resolve
from the chain rather than from live state. All three surfaces work offline;
none require a running server.

### Variables - `bernstein var`

A fleet variable is a named JSON value whose identity is its audit-chain
segment.

```bash
bernstein var set threshold 5        # records a fleet.var_set chain event
bernstein var set threshold 9        # old->new value hashes chained
bernstein var get threshold          # -> 9
bernstein var history threshold      # every write, offline from the chain
```

Each `set` records the old and new value hashes plus a per-name write
ordinal. A claim-time read (in the run path) pins `(name, value_hash,
chain_position)` into the reading task's lineage spine, and replay resolves
that pinned hash - never the live value - so a run replayed after N further
mutations reads byte-identically. When two workers read different values,
`var history` explains it from the chain alone: the write that landed
between their two pinned positions. Values live under
`.sdd/fleet/variables/` (content-addressed blobs plus a name index).

### Connection documents - `bernstein conn`

A connection document is a typed, named record (`prod-github`, `team-slack`)
that task specs reference by name. It carries no secret material - only a
broker secret *reference*, a scope, and connector defaults - and is signed
with the local Ed25519 install identity.

```bash
bernstein conn create prod-github --secret github_pat --scope repo:read
bernstein conn list
bernstein conn rotate prod-github --secret github_pat_v2   # zero spec edits
bernstein conn audit prod-github                           # every resolving task, offline
```

Rotation re-points every consumer at the next mint. Resolution runs only
through the secrets-broker mint path, so the raw secret is minted into a
short-lived token and registered for redaction; it never reaches an agent
environment or artifact. A document copied to another install refuses to
resolve (its signature does not verify against the copy's local identity),
and the refusal is itself an audit event. See
[secrets-broker.md](../security/secrets-broker.md#connection-documents-2550).

### Operating contexts - `bernstein ctx`

An operating context atomically pins server URL, store DSN, adapter
defaults, and a budget-envelope name as one named unit. (The `context`
command name is taken by the worker context-capsule surface (#2545), so this
fleet surface is `ctx`.)

```bash
bernstein ctx create staging --server-url https://staging --set budget=42
bernstein ctx use staging          # atomic activation, recorded on the chain
bernstein ctx show                 # the active context + its settings hash
bernstein ctx list
```

Activation inserts one `context` layer into the precedence chain, between
the project and global layers (see the precedence note below), and records a
`fleet.context_activate` event embedding the canonical effective-settings
hash. Two installs with identical context content produce a byte-identical
canonical document and an equal hash, so a run carries a configuration
identity a verifier recomputes; config drift becomes a detected hash
divergence with a named cause. With no context active, the existing
four-layer precedence and the `bernstein profile` perf command are
unchanged. Contexts live under `.sdd/fleet/contexts/`, with `active.json`
recording the activation.

Precedence with a context active (highest first): session > project >
**context** > global > default.

### Verification

`bernstein audit verify` covers all three families through the HMAC chain
and an additional semantic pillar that checks variable write-ordinal
contiguity, value-hash lineage, and connection reference integrity. See
[audit-log.md](../security/audit-log.md#fleet-config-plane-events).
