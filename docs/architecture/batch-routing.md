# Batch routing

Non-urgent tasks (docs, formatting, simple tests) can run through a
provider's batch API instead of the interactive one, at roughly half the
per-token cost. Batch routing is two steps: a task-shape **classifier**
decides whether a task is a batch *candidate*, then an adapter-capability
**gate** decides whether it can actually be dispatched to a batch endpoint —
a batch-eligible task on an adapter with no batch surface is refused back to
the interactive path rather than silently faked onto one.

There is no dedicated CLI for this; it runs automatically at task
decomposition and dispatch time. `bernstein explain <task>` surfaces whether
a task was marked batch-eligible.

## Step 1 — eligibility classification

`classify_batch_mode(task)` runs at decomposition time and returns a
`BatchClassification` (`mode`, `reason`, `discount_factor`). A task is
**never** batch-eligible when any of these hold:

- `task.role` is `manager`, `architect`, `security`, or `orchestrator`.
- `task.priority == 1` (critical).
- `task.scope == LARGE`.
- `task.complexity == HIGH`.
- `task.task_type` is `RESEARCH` or `UPGRADE_PROPOSAL`.
- The manager explicitly requested `opus` via `task.model`.
- `task.batch_eligible` was explicitly set to `False`.

Past those hard gates, a task is batch-eligible when `task.batch_eligible`
was explicitly set to `True`, or `task.complexity == LOW`, or its
title/description matches a batch-keyword pattern (`doc`, `docstring`,
`readme`, `changelog`, `format`, `lint`, `comment`, `type hint`,
`simple test`, `unit test`, `release note`, `bump version`, and similar).
The discount factor is a flat `0.5` (`BATCH_DISCOUNT_FACTOR`) whenever
eligible, `1.0` otherwise; `apply_batch_discount(cost_usd, classification)`
applies it to a cost estimate.

## Step 2 — capability-gated dispatch

Eligibility alone does not guarantee a batch dispatch. `route_batch(task_id,
adapter, batch_eligible)` (`core/cost/scheduling/batch.py`) checks the
adapter's declared batch-dispatch capability
(`adapters/_contract.py::batch_dispatch_capability`) before committing to a
route:

| `batch_eligible` | Adapter capability | Route | Notes |
|---|---|---|---|
| `False` | any | `interactive` | Not a batch candidate; no refusal reason recorded. |
| `True` | `NATIVE` | `batch` | Dispatched to the provider's batch endpoint. |
| `True` | `NONE` | `interactive` | Refused with `adapter_no_batch_surface`; runs interactively instead of faking a batch path that doesn't exist. |

Only `claude`, `claude_routine`, and `openai_agents` declare `NATIVE` batch
capability today (`_BATCH_CAPABLE_ADAPTERS`); every other adapter — including
unknown or third-party adapters — reports `NONE` by default, so batch-eligible
work is never routed somewhere that cannot honour it.

## How it composes

`route_batch` is a pure function of `(task eligibility, adapter capability)`
with no side effects, so it slots into the deterministic dispatch policy
without special-casing. The live dispatch path
(`core/cost/scheduling/live_dispatch.py`) calls `classify_batch_mode`
directly to decide whether a task counts as batch for scheduling purposes.

## Limitations

- Classification is a fixed set of heuristics (role/priority/scope/complexity
  gates plus a keyword regex) — there is no learned or configurable scoring
  model, and no per-project keyword override.
- The capability list (`_BATCH_CAPABLE_ADAPTERS`) is a hardcoded allowlist in
  source; adding batch support for a new adapter requires a code change, not
  a config change.

## Source

- `src/bernstein/core/tasks/batch_router.py` — `classify_batch_mode`, `BatchClassification`, `apply_batch_discount`.
- `src/bernstein/core/cost/scheduling/batch.py` — `route_batch`, `BatchRouteDecision`.
- `src/bernstein/adapters/_contract.py` — `BatchDispatchCapability`, `batch_dispatch_capability`.
