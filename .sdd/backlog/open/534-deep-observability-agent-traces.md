# 534 — Deep observability: agent decision traces + replay

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium

## Problem

"When an agent takes a 12-step journey to answer a query, you need to understand
every decision point along the way." — This is the #2 developer pain point in
agent frameworks (2026 surveys). Bernstein logs are flat text; there's no way
to trace WHY an agent made a decision, retry a specific step, or replay a failed
task with different parameters.

## Design

### Structured trace format
Each agent execution produces a trace:
```json
{
  "trace_id": "abc123",
  "task_id": "task-456",
  "agent_role": "backend",
  "model": "claude-sonnet-4-6",
  "steps": [
    {"type": "orient", "files_read": ["server.py"], "tokens": 1200, "duration_ms": 340},
    {"type": "plan", "decision": "modify server.py:add_auth_middleware", "confidence": 0.85},
    {"type": "edit", "file": "server.py", "lines_changed": 12, "duration_ms": 890},
    {"type": "verify", "ran_tests": true, "tests_passed": 8, "tests_failed": 0}
  ],
  "total_tokens": 4500,
  "total_cost_usd": 0.02,
  "outcome": "success"
}
```

### Trace capture
- Parse agent output (structured markers in system prompt)
- Capture tool calls, file reads/writes, test runs
- Store in `.sdd/traces/` (JSONL, one per task)

### Trace viewer
- `bernstein trace <task_id>` — CLI trace viewer (Rich tree)
- Web dashboard trace panel (if #526 is built)
- Trace diff: compare successful vs failed runs of similar tasks

### Replay
- `bernstein replay <trace_id>` — re-run task with same context
- `bernstein replay <trace_id> --model opus` — retry with better model
- Useful for debugging: why did this task fail?

### Integration with eval harness
- Traces feed into failure cartography (#517)
- Identify systematic failure patterns across traces
- "Agents fail at step 3 (plan) 40% of the time" -> improve planning prompts

## Files to modify
- `src/bernstein/core/spawner.py` — trace capture from agent output
- New: `src/bernstein/core/traces.py` — trace storage + parsing
- `src/bernstein/cli/main.py` — `bernstein trace` and `bernstein replay` commands
- Agent system prompts — structured output markers

## Completion signal
- Every task execution produces a structured trace
- `bernstein trace <id>` shows step-by-step decision tree
- `bernstein replay <id>` successfully re-executes a task
