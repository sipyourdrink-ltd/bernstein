# Opt-in LLM watcher

The LLM watcher is a side-channel observer that reads the
deterministic loop's events and annotates them with a natural-language
summary. The default model is Anthropic's Haiku. The watcher is
**off by default**; it sits at the top of Bernstein's three-layer
architecture (deterministic orchestrator below, immutable HMAC
chain in the middle, LLM observer above).

## Why it exists

The orchestrator emits a structured event stream every tick: plan
decisions, task spawns, task completions, merge decisions. Reading
that stream raw is feasible for a developer and tedious for an
on-call human. The watcher converts the stream into short
human-readable suggestions: "task T-12 has spawned 4 times with the
same failing fixture, consider escalating", "budget burned 80% in
the first 20 minutes, expect throttling soon".

The watcher is structurally read-only by design:

- The public `observe` API only accepts an immutable, frozen
  `WatcherEvent` snapshot. It receives no orchestrator handle, no
  task store, no agent spawner, no filesystem path.
- The return type is `list[Suggestion]` - pure advisory data.
  Suggestions are never auto-applied. The orchestrator decides
  whether to log, surface, or persist them.
- Failures inside the watcher (LLM adapter exceptions, timeout,
  network) are caught and converted into an empty signal list. A
  misbehaving watcher cannot crash the orchestrator.

## How to enable it

The watcher is disabled by default. The orchestrator emits zero
events and makes zero LLM calls until it is opted in.

```bash
# Enable for the next run
export BERNSTEIN_LLM_WATCHER_ENABLED=1

# Optional: pin a different observer model
export BERNSTEIN_LLM_WATCHER_MODEL=haiku
export BERNSTEIN_LLM_WATCHER_PROVIDER=claude

bernstein run plan.yaml
```

The default observer model is Haiku via the existing Claude
adapter. The model name resolves through the same alias map as the
rest of the orchestrator, so `haiku`, `claude-haiku`, or any
operator-defined alias work.

## What it observes

The watcher subscribes to four event kinds:

| Event | Trigger |
|---|---|
| `plan_decided` | The orchestrator commits a plan for the run. |
| `task_spawned` | A task is claimed and an agent is launched. |
| `task_completed` | A task finishes (success or failure). |
| `merge_decided` | The merge queue selects an outcome for a task. |

Each event is delivered as a frozen `WatcherEvent` carrying the
event kind, the run ID, a timestamp, and a sanitised payload. The
payload is a JSON-serialisable dict; callers must not put callable
objects, file handles, or references to orchestrator-internal
mutable state into the payload.

## Suggestions

The watcher returns a list of `Suggestion` records:

| Field | Meaning |
|---|---|
| `suggestion_id` | Stable identifier for cross-referencing in logs. |
| `run_id` | Run that the originating event belonged to. |
| `detector` | Free-form detector name. Built-in local detectors use names such as `cost_runaway` and `stuck_spawn`; model-generated suggestions use `observer`. |
| `severity` | `info`, `warning`, or `critical`. |
| `rationale` | Short human-readable explanation. |
| `proposed_action` | Suggested next step (informational only - never executed automatically). |
| `cost_usd` | Estimated USD cost attributed to this suggestion; local detector suggestions have no model-call cost. |

When the watcher is enabled, the environment-based factory registers the
built-in local detectors. For each event, they run synchronously before the
model observer. Their suggestions remain in the result if the model returns
an empty response or the observer raises an error; the model may add a
separate `observer` suggestion. These local suggestions are advisory too and
are never applied automatically.


## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `BERNSTEIN_LLM_WATCHER_ENABLED` | `0` | Master switch. Watcher emits nothing unless set to a truthy value (`1`, `true`, `yes`, `on`). |
| `BERNSTEIN_LLM_WATCHER_MODEL` | `haiku` | Observer model alias. |
| `BERNSTEIN_LLM_WATCHER_PROVIDER` | `claude` | Provider for the observer model. |

## Limitations

- The enabled environment factory registers the built-in local detectors:
  `cost_runaway`, `stuck_spawn`, `repeated_failure`,
  `suspicious_tool_mask`, and `audit_chain_break`. The suggestion-review
  CLI remains follow-up work.
- The watcher cannot mutate orchestrator state by construction.
  Suggestions are advisory until a human or a future review
  CLI acts on them.
- Cost is bounded by the per-event LLM call; a runaway observer
  is bounded by the per-run cost cap (see
  [Cost optimisation](../operations/cost-optimization.md)).

## Related

- Source: `src/bernstein/core/observability/llm_watcher.py`
- Cost cap: `bernstein run --max-cost-usd N` (see
  [CLI reference](../reference/cli-reference.md))
- [Capability matrix](../security/capability-matrix.md)
- [Observability overview](../operations/observability-overview.md)
