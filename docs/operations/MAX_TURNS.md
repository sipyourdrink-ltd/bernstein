# Explicit `max_turns` on task create

A task's turn budget depends on the adapter. The standalone
`compute_max_turns` helper estimates a budget from task complexity, model
speed, and the task timeout. The Claude adapter instead uses its own
effort and task-scope calculation or its batch-mode limit by default.
`TaskCreate.max_turns` lets an API caller request an explicit turn count
for adapters that support it.

## When to use it

- You know from experience that a task type needs more (or fewer) turns than
  the adapter's default.
- You're running a benchmark or eval and want a reproducible turn count
  on an adapter that supports explicit counts.
- You want to set a turn limit for a specific task on a supported adapter.

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

The request model accepts an optional integer from 1 to 10,000 inclusive.
Omitting the field or passing `null` keeps the adapter's normal turn budget.
Values that cannot be parsed as an integer in that range fail request
validation (HTTP 422).

## Behavior

There are two distinct calculations to keep in mind:

- The standalone `compute_max_turns(explicit_max_turns=...)` helper bypasses its
  complexity/model/timeout calculation, but clamps the explicit value to its
  configured `min_turns` and `max_turns_cap` bounds (defaults: 5 and 200).
  This helper's bounds are separate from the request field's accepted range.
- The Claude adapter supports the task's explicit count. Its `_build_command`
  uses a positive `explicit_max_turns` verbatim for the CLI `--max-turns` flag;
  it does not apply the helper's bounds. This explicit value replaces the
  adapter's normal turn-count calculation (batch-mode limit or effort base
  multiplied by task-scope multiplier), not the standalone helper's
  complexity/model/timeout calculation. Without an explicit value, the
  adapter uses that normal calculation.

For a direct launch, Bernstein forwards the largest explicit count in
the group of tasks to an adapter that accepts `explicit_max_turns`.
If the adapter does not accept that argument, the spawner logs a warning
and the launch continues with its automatic turn count. The caching
wrapper accepts the argument at its own interface and logs a debug
message when it omits the argument for an unsupported underlying adapter. The sandbox, container, and in-process launch paths do not
use this forwarding logic.

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
