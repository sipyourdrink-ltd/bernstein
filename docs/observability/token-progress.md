# Per-agent token progress

`bernstein status` shows a live token count for every running agent so an
operator can see who is burning context fast, who is near their budget, and
who still has headroom, without opening a single log file.

## What `bernstein status` shows

```bash
bernstein status                  # Rich table output
bernstein status --json           # machine-readable JSON
bernstein status --mode expert    # show all details
bernstein status --mode novice    # minimal output
```

The Active Agents table has a `Tokens` column:

| Display | Meaning |
|---|---|
| `-` | No tokens recorded yet for this agent. |
| `12,480` | Tokens used so far; the agent has no configured budget (`token_budget == 0`, i.e. unlimited). |
| `12,480/50,000` | Tokens used against a configured per-task budget. |

With `--json`, the same numbers are in each entry of the `agents` array as
`tokens_used`, `token_budget`, and `context_utilization_pct`.

## Where the numbers come from

Each running agent session writes token usage to a per-session sidecar file,
`.sdd/runtime/<session_id>.tokens`. Once per orchestrator tick,
`check_token_growth()` reads the bytes appended since the last tick from that
file and writes the cumulative total onto the in-memory `AgentSession.tokens_used`
field (`core/tokens/token_monitor.py`). The task server's `/status` route
serves that live field straight through to `bernstein status`
(`core/routes/status_dashboard.py`).

`token_budget` is not something the agent reports about itself — it is the
per-task budget the orchestrator computed from the task's scope at spawn time
(`_max_tokens_per_task`, `core/agents/spawner_core.py`), stored on the same
`AgentSession` record. A task with no configured scope budget keeps
`token_budget == 0`, which the status view renders as "no budget" rather than
`x/0`.

`context_utilization_pct` is a related but separate figure: percentage of the
model's context window currently in use, updated by the same tick
(`_update_context_window_utilization`).

## Budget nudge

When `tokens_used` crosses 80% of a non-zero `token_budget`
(`_DEFAULT_NUDGE_THRESHOLD_PCT`, `core/tokens/token_monitor.py`), the monitor
sends the agent a one-time continuation nudge — a short message asking it to
wrap up — and marks the session so the nudge does not repeat. The nudge is
purely a runtime hint sent to the agent; nothing in the CLI display changes
when it fires.

## Relationship to the agent-level circuit breaker

Progress tracking is passive: it counts and displays, and the nudge is
advisory. Enforcement is a separate mechanism — the agent-level circuit
breaker (`core/observability/circuit_breaker.py`) can terminate an agent that
exceeds its session token limit. See
[Observability overview - Circuit breakers](../operations/observability-overview.md#circuit-breakers)
for what happens after the budget is exceeded.

## Limitations

- A task without a scope-derived budget shows only a running total, never a
  ratio — there is no operator-facing way to force `bernstein status` to
  display an arbitrary budget for an unbudgeted task.
- The tick-based sidecar read means the displayed number can lag the agent's
  true in-flight usage by up to one orchestrator tick.

## Source

- `src/bernstein/core/tokens/token_monitor.py` — `check_token_growth`, `read_tokens`, `_handle_budget_nudge`
- `src/bernstein/core/tasks/models.py` — `AgentSession.tokens_used`, `AgentSession.token_budget`
- `src/bernstein/core/routes/status_dashboard.py` — `/status` payload assembly
- `src/bernstein/cli/status.py`, `src/bernstein/cli/commands/status_cmd.py`, `src/bernstein/cli/ui.py` — `bernstein status` command and table rendering
- `src/bernstein/core/agents/spawner_core.py` — per-task token budget assignment
