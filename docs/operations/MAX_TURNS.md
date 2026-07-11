# Explicit `max_turns` on task create

By default, an agent's turn budget is computed from task complexity, model
speed, and the task timeout (`compute_max_turns` in
`bernstein.core.agents.claude_max_turns`): higher complexity gets more turns,
and a fast model gets more turns than a slow one within the same timeout
window. `TaskCreate.max_turns` lets an API caller bypass that computation
entirely and pin an exact turn count.

## When to use it

- You know from experience that a task type needs more (or fewer) turns than
  the complexity-based default.
- You're running a benchmark or eval and need a deterministic, reproducible
  turn budget across runs regardless of which model handles the task.
- You want to hard-cap runaway agents on a specific task without changing
  the global `max_turns_cap`.

## Usage

```json
POST /tasks
{
  "title": "...",
  "description": "...",
  "role": "backend",
  "max_turns": 60
}
```

Leave `max_turns` unset (the default, `null`) to keep the automatic
complexity/model/timeout computation.

Accepted values are 1 to 10000 inclusive; zero, negative, and larger
values are rejected at request validation time because the number is
passed verbatim to the CLI `--max-turns` flag.

## Behavior

When `max_turns` is set on the task:

- `compute_max_turns(explicit_max_turns=...)` returns that value verbatim —
  complexity, model tier, and timeout are still recorded on the
  `MaxTurnsConfig` for logging, but none of them affect the number.
- `constrained_by_timeout` is always `False` for an explicit value (the
  caller is assumed to have already accounted for timeout).
- The spawner threads the value through as `explicit_max_turns` to the
  active adapter, but only if that adapter's `spawn()` signature accepts an
  `explicit_max_turns` parameter. Adapters that don't support it log a
  warning and silently fall back to auto-computed turns — the task still
  runs, just without the explicit cap. Check the adapter's `spawn()`
  signature if you need to confirm support before relying on this for a
  hard limit.

## Schema

`max_turns: int | None` on `TaskCreate` (`src/bernstein/core/server/server_models.py`)
and on the internal `Task` dataclass (`src/bernstein/core/tasks/models.py`).
`None` means "use the automatic computation."

## Code pointers

| File | What it does |
|------|--------------|
| `src/bernstein/core/agents/claude_max_turns.py` | `compute_max_turns`, `MaxTurnsConfig` |
| `src/bernstein/core/server/server_models.py` | `TaskCreate.max_turns` field |
| `src/bernstein/core/tasks/models.py` | `Task.max_turns` field, dict round-trip |
| `src/bernstein/core/agents/spawner_core.py` | Threads `explicit_max_turns` to the adapter if supported |
