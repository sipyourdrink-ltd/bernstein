# .sdd/ — Bernstein persistent state

Everything Bernstein needs to remember across a process restart lives
here. Agent memory is transient; the orchestrator is stateless; this
directory is the single source of truth. Safe to commit selectively (see
`.gitignore` below) or wipe to force a fresh run.

## Directory layout

| Path | Owner | Purpose |
|------|-------|---------|
| `backlog/open/*.yaml` | task store | Tasks not yet claimed. |
| `backlog/claimed/`, `in_progress/`, `done/`, `closed/` | task store | Task lifecycle stages; tasks move between dirs atomically. |
| `backlog/_duplicates/`, `deferred/`, `manual/`, `issues/` | task store | Special-case tasks outside the normal flow. |
| `runtime/checkpoints/checkpoint-{id}.json` | orchestrator | Atomic full-snapshot (`checkpoint.Checkpoint`): task graph, agent sessions, cost, WAL position. Used for crash recovery. |
| `runtime/session.json` | orchestrator | Fast-resume session state written on graceful stop. |
| `runtime/wal.jsonl` | WAL | Write-ahead log of task-state transitions; replayed on startup to recover from crash between checkpoints. |
| `runtime/startup_gates.json` | quality gates | Gate provenance and cache status captured at startup. |
| `runtime/latched_flags.json` | config | Session-stable flags latched at startup. |
| `runtime/bridge_lineage.jsonl` | agents | Bridge transport lifecycle events (rotated). |
| `runtime/task_notifications.jsonl` | agents | Structured status notifications from agents (rotated). |
| `sessions/{ts}-checkpoint.json` | CLI | Operator-visible progress slice (`checkpoint.PartialState`): goal, completed/in-flight/next, cost, git SHA. Written by `bernstein checkpoint`. |
| `sessions/{ts}-wrapup.json` | CLI | End-of-session brief: changes summary, learnings, handoff. |
| `agents.json`, `agents/`, `heartbeats/` | agent registry | Currently-known agents and their liveness state. |
| `costs/`, `completion_budgets.json` | cost tracker | Per-agent cost ledgers and enforced budgets. |
| `config.yaml`, `config_snapshot.json`, `config_state.json` | config | Project config, last-seen snapshot, resolved state. |
| `gates/`, `hooks/` | quality / policy | Gate results and hook invocation logs. |
| `memory/`, `knowledge/`, `index/` | knowledge | Persistent memory store, knowledge graph, embedding index. |
| `metrics/`, `traces/`, `logs/` | observability | Prometheus samples, OTel traces, rotated log files. |
| `incidents/`, `signals/`, `routing/` | runtime | Incident timelines, control-plane signals, routing decisions. |
| `auth/`, `attestations/` | security | HMAC keys, audit-chain attestations. |
| `worktrees/` | git | Per-agent git worktrees, cleaned up on reap. |
| `decisions/`, `research/`, `upgrades/` | planning | Architect decisions, background research notes, staged upgrades. |
| `access.jsonl[.N]` | server | HTTP request log, rotated. |

## Two checkpoint concepts (audit-084)

1. **`checkpoint.Checkpoint`** — the canonical crash-recovery snapshot.
   Atomic, immutable, written periodically by the orchestrator to
   `runtime/checkpoints/`. Carries the full task graph, agent sessions,
   cost accumulator, and WAL position.
2. **`checkpoint.PartialState`** (legacy alias: `session.CheckpointState`)
   — the operator-visible progress slice. Non-atomic, written by
   `bernstein checkpoint` to `sessions/{ts}-checkpoint.json`. Safe to
   lose; the canonical `Checkpoint` + WAL replay remain the source of
   truth for recovery.

## Recovery guarantees

On startup, the orchestrator looks for the most recent valid
`runtime/checkpoints/checkpoint-*.json`, loads the task graph and cost
state, then replays `runtime/wal.jsonl` entries after the checkpoint's
`wal_position` to restore in-flight task state. If no checkpoint exists,
it bootstraps from `backlog/` alone. A graceful stop additionally writes
`runtime/session.json` so the next start can skip the manager planning
phase.

## Wiping state

- `rm -rf .sdd/runtime` — force full bootstrap (keeps backlog and memory).
- `rm -rf .sdd` — full reset (rare; prefer `bernstein run --fresh`).
