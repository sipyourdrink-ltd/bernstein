# WORKFLOW: Reactive 413 Compaction and Budget Tracking
**Version**: 1.0
**Date**: 2026-04-04
**Status**: Stable
**Implements**: p0_c2_020426 — reactive-compact-as-413-fallback-handler, task-budget-tracking-across-compaction-events

---

## Overview

When a provider returns HTTP 413 (or an equivalent "prompt too large" error), the orchestrator
intercepts the failure, runs the context compaction pipeline to shrink the task description,
patches the retry task with the compacted prompt and a budget hint, and re-queues it once.
Per-task token budgets are reconciled across the compaction boundary so cost telemetry and
remaining-budget hints shown to the retry agent remain accurate.

---

## Actors

| Actor | Role |
|---|---|
| `RateLimitTracker` | Scans agent log on exit; classifies failure as `context_overflow` |
| `handle_orphaned_task` | Entry point when an agent dies; dispatches to `_try_compact_and_retry` |
| `_try_compact_and_retry` | Runs `CompactionPipeline`, reconciles budget, queues retry |
| `CompactionPipeline` | Strips media blocks, summarizes context, fires pre/post hooks |
| `TokenBudgetManager` | Holds per-task `TokenBudget`; called before and after compaction |
| `TokenBudget` | Tracks pre-compaction usage so total logical spend stays accurate |
| `_patch_retry_with_compaction` | Patches retry task: compacted description + meta-messages |

---

## Detection — Context Overflow Patterns

`RateLimitTracker.detect_failure_type(log_path)` scans the tail of the agent log for the
following patterns (case-insensitive):

```
413
prompt is too long
prompt too long
context window
context_length_exceeded
max_tokens
maximum context length
token limit exceeded
request too large
payload too large
input is too long
prompt_too_long
context length exceeded
PromptTooLongError
```

When matched, `detect_failure_type` returns `"context_overflow"`.  Rate-limit patterns (429)
take priority — if both appear in the log, the task is treated as rate-limited, not 413.

---

## Retry Bound

```python
_COMPACT_MAX_RETRIES: int = 1  # src/bernstein/core/agent_lifecycle.py
```

The orchestrator counts `"CONTEXT COMPACTION"` strings in `task.meta_messages` to detect prior
compaction retries.  Once `prior_compact_retries >= _COMPACT_MAX_RETRIES`, the task is failed
permanently with reason `"Context overflow: compaction retries exhausted"`.

**To allow more retries** (not recommended — most prompts cannot be usefully compacted twice):
patch `_COMPACT_MAX_RETRIES` in `agent_lifecycle.py`.

---

## Budget Adjustment Rules

### Before compaction

```python
_task_budget.record_pre_compaction(tokens_before)
```

Snapshots `used_tokens` (estimated from `len(description) // 4`) into `pre_compact_used`.
This accumulates across multiple compaction events:

```
pre_compact_used += tokens_before
compaction_count += 1
```

### After compaction

```python
_task_budget.reconcile_post_compaction()
```

Recomputes `remaining_tokens` against the full logical spend:

```
remaining_tokens = max(0, budget_tokens - (pre_compact_used + used_tokens))
```

This prevents the retry agent from believing it has a fresh full budget after compaction.

### Effective remaining hint

`effective_remaining = budget_tokens - total_logical_spend()` is formatted and injected as
a meta-message into the retry task:

```
BUDGET EFFECTIVE REMAINING: ~42K tokens remaining after accounting for context consumed
before compaction.  Plan work to fit.
```

The retry agent sees this in the **Operational nudges** section of its spawn prompt.

---

## Per-Task Budget Defaults

Configured in `token_budget.py`:

| Complexity | Default budget (tokens) |
|---|---|
| `small` | 10,000 |
| `medium` | 25,000 |
| `large` | 50,000 |
| `xl` | 100,000 |

Override via `TokenBudgetManager(workdir, budgets={...})` or by patching `DEFAULT_TOKEN_BUDGETS`.

A task with `token_budget: 0` in its model record is treated as **unlimited** — no budget hint is
injected.

---

## Compaction Pipeline Stages

`CompactionPipeline.execute()` runs the following in order:

1. **Pre-compact hooks** — plugin callbacks receive `PreCompactPayload`
2. **Strip media** — removes base64 image and document blocks
3. **LLM summary** (optional) — calls `summarize_context()` if an LLM client is wired in
4. **Post-compact hooks** — plugin callbacks receive `PostCompactPayload`

If any stage raises, the pipeline aborts and the task is failed immediately (no retry queued).

---

## Metrics

| Metric key | When emitted |
|---|---|
| `context_overflow_compacted` | Compaction succeeded, retry queued |
| `context_overflow_compact_failed` | Compaction limit exhausted or pipeline error |

Both are emitted via `emit_orphan_metrics(workdir, ...)` with `error_type` label.

---

## WAL Audit Entry

On successful compaction a WAL entry is written:

```json
{
  "decision_type": "context_overflow_compacted",
  "inputs": {
    "task_id": "...",
    "agent_id": "...",
    "tokens_before": 12345,
    "tokens_after": 4321
  },
  "output": {
    "correlation_id": "...",
    "tokens_saved": 8024,
    "compacted": true
  },
  "actor": "agent_lifecycle"
}
```

---

## Operator Checklist

- **Increasing the retry limit**: edit `_COMPACT_MAX_RETRIES` in `agent_lifecycle.py`.  Values
  above `2` rarely help — if the first compaction doesn't reduce the prompt enough, subsequent
  ones won't either.
- **Adjusting per-complexity budgets**: update `DEFAULT_TOKEN_BUDGETS` in `token_budget.py` or
  pass a custom `budgets` dict to `TokenBudgetManager`.
- **Disabling budget hints**: set `token_budget: 0` on tasks where unlimited context is acceptable.
- **Disabling compaction on certain providers**: use the `ModelPolicy` `blocked_providers` list
  so 413-prone providers are never selected, pre-empting the overflow entirely.
- **Debugging a missed detection**: set log level to `DEBUG` and search for
  `"scan_log_for_context_overflow"` — the function logs the tail of the agent log it inspects.
