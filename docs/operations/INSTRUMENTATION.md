# Per-agent run instrumentation

Orchestrator-level phase/task timing (`.sdd/runs/<run_id>/summary.json`) tells
you *what* happened across a run, but not what happened *inside* a single
agent process. `RunInstrumenter` (`bernstein.core.instrumentation`) adds that
layer: every LLM call, every tool call, and every message an agent produces,
recorded as JSONL so it can be tailed live or replayed after the fact.

This is observe-only. Every public method is wrapped in a broad
`try/except` and degrades to a logged warning on failure — instrumentation
must never crash or block the agent run it's watching.

## Where files are written

One directory per agent, created on first write:

```
.sdd/runs/<run_id>/tasks/<task_id>/agents/<agent_id>/
  llm-calls.jsonl
  tool-calls.jsonl
  conversation.jsonl
```

`resolve_agent_dir(workdir, run_id, task_id, agent_id)` computes this path.
`run_id` reaches the agent subprocess via the `BERNSTEIN_RUN_ID` environment
variable — it must stay on the adapter env-isolation allowlist
(`bernstein.adapters.env_isolation._BASE_ALLOWLIST`), or a filtered
subprocess env silently drops it and the agent falls back to `"unknown"`.

## Enabling it

Currently wired into the OpenAI Agents SDK runner
(`bernstein.adapters.openai_agents_runner`) via `init_instrumenter(...)` at
session start and `get_instrumenter()` at each hook point. Before
`init_instrumenter` runs, `get_instrumenter()` returns a no-op
`_NullInstrumenter` so call sites never need an `if instrumenter:` guard.

## Schema

### `llm-calls.jsonl` — one line per LLM API call

| Field | Type | Notes |
|-------|------|-------|
| `call_id` | str | dedup key |
| `ts_start`, `ts_end` | ISO-8601 str | |
| `wall_ms` | float\|null | computed from ts_start/ts_end if not given |
| `model` | str | |
| `endpoint` | str\|null | |
| `prompt_tokens`, `completion_tokens`, `total_tokens` | int\|null | |
| `status` | str | `"ok"` by default |
| `error` | str\|null | |
| `ts_ttft` | str | only present if the call streamed |
| `tokens_per_sec` | float | only present if the call streamed — never fabricated for non-streaming calls |

### `tool-calls.jsonl` — one line per tool invocation

| Field | Type | Notes |
|-------|------|-------|
| `call_id` | str | dedup key |
| `ts_start`, `ts_end`, `wall_ms` | see above | |
| `tool` | str | tool name |
| `args` | dict | truncated to 500 chars per string value (`_ARG_VALUE_TRUNCATE_CHARS`) — never the full tool result, only the call's own arguments |
| `success` | bool | |
| `error` | str\|null | |

### `conversation.jsonl` — one line per new message

| Field | Type | Notes |
|-------|------|-------|
| `idx` | int | message index, used as dedup key |
| `role` | str | |
| `content_length` | int | shape only — message content is never recorded, by design |
| `tool_calls` | list[str] | present if the message triggered tool calls |
| `ts` | str | ISO-8601 |

## Design notes

- One `RunInstrumenter` instance per `(run_id, task_id, agent_id)` — the
  module-level singleton works because each OpenAI Agents SDK process is
  already one-agent-per-process, so "one instance per process" and "one
  singleton" coincide.
- Long string values (e.g. a full file body passed to `write_file`) are
  truncated recursively through nested dicts/lists so `tool-calls.jsonl`
  never bloats on a single large argument.

## Code pointers

| File | What it does |
|------|--------------|
| `src/bernstein/core/instrumentation.py` | `RunInstrumenter`, singleton, truncation helpers |
| `src/bernstein/adapters/openai_agents_runner.py` | Hook points (`emit_event`, conversation logging) |
