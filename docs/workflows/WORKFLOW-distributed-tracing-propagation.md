# WORKFLOW: Distributed Tracing Context Propagation
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-117 — Distributed tracing context propagation across all Bernstein components

---

## Overview

Propagate a single W3C `traceparent` header from CLI invocation through every component in the task lifecycle — task server, orchestrator, spawner, agent subprocess, quality gates, and merge queue — so that one trace ID spans the entire life of a task from creation to merge. This enables end-to-end latency analysis, failure correlation, and cross-component debugging in any W3C-compatible backend (Jaeger, Datadog, Grafana Tempo, Zipkin).

**Distinct from**: ORCH-014 (tick phase spans — already shipped), CLAUDE-020 (Claude trace correlation — agent-side only). This workflow is the glue that connects them.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| CLI (`run_cmd.py`) | Creates the root trace context for the entire run |
| Task Server (FastAPI routes) | Receives and propagates `traceparent` header on every request; creates child spans per endpoint |
| Orchestrator (`orchestrator.py`) | Carries trace context through tick phases; passes it to spawner |
| Spawner (`spawner.py`) | Generates child span for spawn operation; injects `TRACEPARENT` env var into agent subprocess |
| Env Isolation (`env_isolation.py`) | Must allowlist `TRACEPARENT`, `BERNSTEIN_TRACE_ID`, `BERNSTEIN_SPAN_ID` |
| Agent (Claude Code / Codex / Gemini / etc.) | Receives trace context via environment; includes `traceparent` header in task server HTTP calls |
| Quality Gates (`quality_gates.py`, `quality_gate_coalescer.py`) | Receives trace context from orchestrator; creates child spans per gate type |
| Merge Queue (`merge_queue.py`) | Receives trace context via `MergeJob`; creates child span for merge operation |
| Correlation Logger (`correlation.py`) | Injects trace_id, span_id, task_id into every structured log record |
| Trace Persistence (`trace_correlation.py`) | Writes `CorrelationRecord` linking Bernstein trace → agent session trace |
| OTel Exporter (`telemetry.py`) | Ships spans to configured backend (Jaeger, Datadog, etc.) |

---

## Prerequisites

- OpenTelemetry SDK optional but recommended (graceful no-op when absent — existing behavior)
- `src/bernstein/core/trace_correlation.py` already implements `generate_trace_context()`, `format_traceparent()`, `parse_traceparent()`, `build_correlation_env()` — all currently **unused**
- `src/bernstein/core/correlation.py` already implements `CorrelationContext`, `CorrelationFilter`, `create_context()`, `set_current_context()` — all currently **unused**
- `src/bernstein/core/telemetry.py` already implements `start_span()`, `get_tracer()` — used by tick telemetry only
- Task server must be running and healthy

---

## Trigger

**Primary**: `bernstein run` CLI invocation (with or without a plan file)
**Secondary**: Any external `POST /tasks` call that includes a `traceparent` header (adopt incoming context rather than generating a new root)

---

## Workflow Tree

### STEP 1: Root Trace Context Creation (CLI)
**Actor**: `run_cmd.py` → `conduct()`
**Action**: Generate root W3C trace context at CLI entry point before starting task server
**Timeout**: N/A (synchronous, local)
**Input**: CLI arguments (goal, plan file, model override, etc.)
**Output on SUCCESS**: `TraceContext(trace_id=<32hex>, span_id=<16hex>, trace_flags="01")` stored in module-level state → GO TO STEP 2

**Implementation site**: `src/bernstein/cli/run_cmd.py`, inside `conduct()`, before `_start_server()`

**Calls**:
```python
from bernstein.core.trace_correlation import generate_trace_context, format_traceparent
root_ctx = generate_trace_context()
```

**Observable states**:
- Customer sees: CLI banner with `trace_id=<hex>` printed for external correlation
- Operator sees: N/A (not yet logged)
- Database: N/A
- Logs: `[cli] run started trace_id=<32hex>`

---

### STEP 2: Orchestrator Receives Root Context
**Actor**: `orchestrator.py` → `Orchestrator.__init__()` or `Orchestrator.run()`
**Action**: Store root `TraceContext` on orchestrator instance; create `CorrelationContext` with `stage="orchestrator"`
**Timeout**: N/A (synchronous init)
**Input**: `TraceContext` from Step 1
**Output on SUCCESS**: `self._trace_context` set; `CorrelationContext` set in `ContextVar` → GO TO STEP 3

**Implementation site**: `src/bernstein/core/orchestrator.py`, `__init__()` or `run()` method

**Calls**:
```python
from bernstein.core.correlation import create_context, set_current_context
ctx = create_context(task_id="orchestrator")  # root-level
set_current_context(ctx)
```

**Observable states**:
- Customer sees: N/A
- Operator sees: Orchestrator log lines now include `correlation_id=<uuid>` via `CorrelationFilter`
- Database: N/A
- Logs: `[orchestrator] initialized correlation_id=<uuid> trace_id=<32hex>`

---

### STEP 3: Tick Phases Carry Trace Context
**Actor**: `orchestrator.py` → `tick()` → `_tick_internal()`
**Action**: Each tick creates a child span under the root trace. Tick telemetry (`TickTelemetryTracker`) already creates OTel spans — wire them to use the root trace as parent.
**Timeout**: Tick budget (configurable, default 30s)
**Input**: `self._trace_context` from Step 2
**Output on SUCCESS**: Tick span with `parent_span_id` pointing to root → GO TO STEP 4 (for each task claimed)

**Implementation site**: `src/bernstein/core/tick_telemetry.py` → `tick_span()`, wire `parent` parameter from `self._trace_context.span_id`

**Observable states**:
- Customer sees: N/A
- Operator sees: Tick spans in tracing backend with correct parent-child nesting
- Database: N/A
- Logs: `[orchestrator] tick=N trace_id=<32hex> span_id=<16hex>`

---

### STEP 4: Task Claim Propagates Context via HTTP Header
**Actor**: `task_lifecycle.py` → `claim_and_spawn_batches()`
**Action**: When claiming tasks via `POST /tasks/claim-batch`, include `traceparent` header in the HTTP request
**Timeout**: HTTP timeout (default 10s)
**Input**: Trace context from orchestrator; task IDs to claim
**Output on SUCCESS**: Tasks claimed; server logs the incoming `traceparent` → GO TO STEP 5

**HANDOFF: Orchestrator → Task Server**
```
PAYLOAD (HTTP headers):
  traceparent: "00-{trace_id}-{span_id}-{trace_flags}"
PAYLOAD (HTTP body):
  { "agent_id": "string", "task_ids": ["string"], "claimed_by_session": "string" }
SUCCESS RESPONSE: { "claimed": [...], "failed": [...] }
FAILURE RESPONSE: { "ok": false, "error": "string", "code": "ERROR_CODE", "retryable": true }
TIMEOUT: 10s — treated as FAILURE → retry x2 with 2s backoff → ABORT (task remains unclaimed)
ON FAILURE: Tasks remain in "open" state; next tick retries
```

**Implementation sites**:
- Client: `src/bernstein/core/task_lifecycle.py`, wherever `httpx` calls are made for claim
- Server: `src/bernstein/core/routes/tasks.py` → `claim_batch()`, extract `traceparent` from `request.headers`

**Observable states**:
- Customer sees: N/A
- Operator sees: Task server access logs include `traceparent`; task claim span linked to orchestrator tick span
- Database: Task status transitions to "claimed"
- Logs: `[task_server] claim_batch trace_id=<32hex> task_ids=[...]`

---

### STEP 5: Spawner Generates Child Span and Injects Env Vars
**Actor**: `spawner.py` → `spawn_for_tasks()`
**Action**: Create a new child `TraceContext` (same `trace_id`, new `span_id`) for the agent subprocess. Build environment variables via `build_correlation_env()`. Pass to adapter.
**Timeout**: Spawn timeout (configurable, default 30s)
**Input**: Parent `TraceContext` from orchestrator; task batch
**Output on SUCCESS**: Agent process started with `TRACEPARENT`, `BERNSTEIN_TRACE_ID`, `BERNSTEIN_SPAN_ID` in environment → GO TO STEP 6

**Implementation sites**:
- `src/bernstein/core/spawner.py` → `spawn_for_tasks()`, after model routing, before `adapter.spawn()`
- `src/bernstein/core/trace_correlation.py` → `build_correlation_env()` (already implemented, currently unused)

**Calls**:
```python
child_ctx = TraceContext(
    trace_id=parent_ctx.trace_id,       # same trace
    span_id=secrets.token_hex(8),        # new span for this agent
    trace_flags=parent_ctx.trace_flags,
)
correlation_env = build_correlation_env(child_ctx)
# correlation_env = {
#   "TRACEPARENT": "00-<trace_id>-<span_id>-01",
#   "BERNSTEIN_TRACE_ID": "<trace_id>",
#   "BERNSTEIN_SPAN_ID": "<span_id>",
# }
```

**Output on FAILURE**:
- `FAILURE(spawn_timeout)`: Agent process did not start within timeout → retry x1 → mark task "failed", no cleanup needed (nothing created yet)
- `FAILURE(adapter_error)`: Adapter raised exception → log error, mark task "failed"

**Observable states**:
- Customer sees: N/A
- Operator sees: Spawn span in tracing backend with agent `session_id` as attribute
- Database: Task status is "claimed" (transitions to "in_progress" when agent reports first heartbeat)
- Logs: `[spawner] spawned session=<session_id> trace_id=<32hex> child_span_id=<16hex>`

---

### STEP 6: Environment Isolation Allowlists Trace Variables
**Actor**: `env_isolation.py` → `build_filtered_env()`
**Action**: Include `TRACEPARENT`, `BERNSTEIN_TRACE_ID`, `BERNSTEIN_SPAN_ID` in the environment passed to agent subprocesses
**Timeout**: N/A (synchronous)
**Input**: `correlation_env` dict from Step 5; existing `extra_keys` list
**Output on SUCCESS**: Agent subprocess environment contains trace variables → GO TO STEP 7

**Implementation site**: `src/bernstein/adapters/env_isolation.py` — add to `_BASE_ALLOWLIST` **or** pass as `extra_keys` from each adapter's `spawn()` method

**Decision point**: Adding to `_BASE_ALLOWLIST` is simpler and ensures all adapters (claude, codex, gemini, etc.) propagate traces without per-adapter changes. Recommended approach.

**Observable states**:
- Customer sees: N/A
- Operator sees: N/A (internal plumbing)
- Database: N/A
- Logs: N/A (env filtering is silent)

---

### STEP 7: Agent Includes traceparent in Task Server Calls
**Actor**: Agent subprocess (Claude Code, Codex, Gemini, etc.)
**Action**: Agent's generated `curl` commands (injected via spawn prompt) include `-H "traceparent: $TRACEPARENT"` header in all task server HTTP calls (progress, complete, fail, bulletin)
**Timeout**: Per-call HTTP timeout (agent-controlled)
**Input**: `TRACEPARENT` environment variable
**Output on SUCCESS**: Task server receives `traceparent` on every agent → server HTTP call → GO TO STEP 8

**Implementation site**: `src/bernstein/core/spawn_prompt.py` or `src/bernstein/core/spawner.py` → `_render_auth_section()` — add traceparent header to the curl template injected into agent prompts

**Current curl template** (in spawner prompt):
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{task_id}/complete \
  -H "Authorization: Bearer $(cat /path/to/token)" \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Done"}'
```

**Modified curl template**:
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{task_id}/complete \
  -H "Authorization: Bearer $(cat /path/to/token)" \
  -H "traceparent: $TRACEPARENT" \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Done"}'
```

**Observable states**:
- Customer sees: N/A
- Operator sees: Agent HTTP calls in tracing backend linked to parent trace
- Database: N/A
- Logs: `[task_server] complete task_id=<id> trace_id=<32hex>` (extracted from incoming `traceparent`)

---

### STEP 8: Task Server Extracts and Logs Incoming traceparent
**Actor**: Task Server (FastAPI middleware or per-route extraction)
**Action**: On every incoming request, extract `traceparent` header. If present, parse it and (a) create a server-side child span, (b) inject `trace_id` into structured logs via `CorrelationFilter`.
**Timeout**: N/A (per-request, synchronous)
**Input**: HTTP request with `traceparent` header
**Output on SUCCESS**: Server span created with correct parent; logs enriched with `trace_id` → GO TO STEP 9 (on task completion)

**Implementation option A — FastAPI middleware** (recommended for uniform coverage):
```python
@app.middleware("http")
async def trace_context_middleware(request: Request, call_next):
    traceparent = request.headers.get("traceparent")
    if traceparent:
        ctx = parse_traceparent(traceparent)
        if ctx:
            correlation = create_context(task_id=request.path_params.get("task_id", ""))
            set_current_context(correlation)
    response = await call_next(request)
    return response
```

**Implementation option B — Per-route extraction** (existing `start_span()` calls already exist in `tasks.py`):
- Add `traceparent` extraction inside each `start_span()` block

**Observable states**:
- Customer sees: N/A
- Operator sees: All task server spans nested under the correct root trace in tracing backend
- Database: N/A
- Logs: `[task_server] <endpoint> trace_id=<32hex> task_id=<id>`

---

### STEP 9: Quality Gates Receive Trace Context
**Actor**: `quality_gate_coalescer.py` → `run()`; `quality_gates.py` → individual gate runners
**Action**: Orchestrator passes current `TraceContext` to gate runner. Each gate type (lint, type-check, test, intent-verify, etc.) creates a child span.
**Timeout**: Per-gate timeout (configurable per gate type)
**Input**: `TraceContext` from orchestrator (same trace_id, new span per gate)
**Output on SUCCESS**: Gate results with span IDs for timing analysis → GO TO STEP 10

**HANDOFF: Orchestrator → Quality Gate Runner**
```
PAYLOAD (function call):
  trace_context: TraceContext  # parent span for this gate run
  task: Task                   # the completed task
  gate_config: QualityGatesConfig
SUCCESS: GateReport with per-gate pass/fail + span_id per gate
FAILURE: Gate runner exception → log with trace_id, mark gate as errored
TIMEOUT: Per-gate timeout → treat as gate failure
ON FAILURE: Gate result = failed; merge blocked if hard_block=True
```

**Implementation site**: `src/bernstein/core/quality_gate_coalescer.py` → `run()` method; pass `trace_context` parameter

**Observable states**:
- Customer sees: N/A
- Operator sees: Per-gate spans (lint: 2.3s, test: 15.1s, intent-verify: 4.7s) nested under task trace
- Database: Gate results written to `.sdd/runtime/gates/{task_id}.json`
- Logs: `[quality_gates] gate=lint trace_id=<32hex> passed=true duration_ms=2300`

**Output on FAILURE**:
- `FAILURE(gate_timeout)`: Gate subprocess exceeded timeout → `GateResult(passed=False, error="timeout")` → if `hard_block`, task stays in post-gate limbo; operator alert
- `FAILURE(gate_crash)`: Gate subprocess crashed → same as timeout handling
- `FAILURE(gate_rejected)`: Gate ran successfully but reported violations → `GateResult(passed=False, findings=[...])` → merge blocked, fix task created

---

### STEP 10: Merge Queue Carries Trace Context
**Actor**: `merge_queue.py` → `MergeQueue.enqueue()` / `dequeue()`
**Action**: `MergeJob` includes `trace_id` and `span_id`. Merge operation creates a child span. Merge success/failure is recorded in the trace.
**Timeout**: Merge timeout (configurable, default 60s for git operations)
**Input**: `MergeJob` with trace context; branch to merge
**Output on SUCCESS**: Branch merged; final span closed; trace complete → END

**HANDOFF: Orchestrator → Merge Queue**
```
PAYLOAD (enqueue call):
  session_id: str
  task_id: str
  task_title: str
  trace_id: str      # NEW FIELD
  parent_span_id: str # NEW FIELD
SUCCESS: Branch merged to base; MergeJob dequeued
FAILURE: Merge conflict detected → conflict resolver task created (inherits trace_id)
TIMEOUT: 60s → treat as failure → retry x1 → create conflict resolver task
ON FAILURE: Branch not merged; task remains "done" but unmerged; operator alert
```

**Implementation site**: `src/bernstein/core/merge_queue.py` → add `trace_id` and `parent_span_id` fields to `MergeJob` dataclass

**Observable states**:
- Customer sees: N/A
- Operator sees: Merge span in tracing backend; full trace from CLI → merge visible end-to-end
- Database: Task status "done"; branch merged
- Logs: `[merge_queue] merged session=<session_id> trace_id=<32hex> duration_ms=1200`

**Output on FAILURE**:
- `FAILURE(merge_conflict)`: `ConflictCheckResult(has_conflicts=True, conflicting_files=[...])` → create resolver task with same `trace_id` (trace continues into resolution)
- `FAILURE(merge_timeout)`: git operations exceeded 60s → retry x1 → create resolver task

---

### STEP 11: Trace Persistence and Finalization
**Actor**: `trace_correlation.py` → `save_correlation()`
**Action**: When an agent session completes (success or failure), write a `CorrelationRecord` to `trace_correlations.jsonl` linking the Bernstein trace_id to the agent's session_id and any agent-side trace IDs.
**Timeout**: N/A (file append, <1ms)
**Input**: `CorrelationRecord` with trace_id, session_id, task_ids, agent_trace_id (if available)
**Output on SUCCESS**: Record persisted → END

**Implementation site**: `src/bernstein/core/spawner.py` → agent session finalization (existing `_traces` dict); call `save_correlation()` which is already implemented but unused

**Observable states**:
- Customer sees: N/A
- Operator sees: `trace_correlations.jsonl` has complete linkage for post-hoc analysis
- Database: N/A (file-based)
- Logs: `[trace_correlation] saved correlation trace_id=<32hex> session_id=<session_id>`

---

## State Transitions

```
[no_trace] -> (CLI starts) -> [root_trace_created]
[root_trace_created] -> (orchestrator init) -> [trace_propagating]
[trace_propagating] -> (tick claim) -> [trace_in_agent]
[trace_in_agent] -> (agent completes) -> [trace_in_gates]
[trace_in_gates] -> (gates pass) -> [trace_in_merge]
[trace_in_merge] -> (merge succeeds) -> [trace_finalized]
[trace_in_merge] -> (merge conflicts) -> [trace_in_resolver] -> [trace_in_merge] (retry)
[trace_in_agent] -> (agent fails) -> [trace_finalized_with_failure]
[trace_in_gates] -> (gate rejects) -> [trace_finalized_with_rejection]
```

---

## Handoff Contracts

### CLI → Orchestrator (in-process)
**Mechanism**: Constructor parameter or module-level state
**Payload**: `TraceContext(trace_id, span_id, trace_flags)`
**Failure**: Cannot fail (same process)

### Orchestrator → Task Server (HTTP)
**Endpoint**: All task server endpoints (`POST /tasks/claim-batch`, `POST /tasks/{id}/complete`, etc.)
**Header**: `traceparent: 00-{trace_id}-{span_id}-{trace_flags}`
**Failure**: HTTP timeout → retry with backoff; header simply missing if not set (graceful degradation)

### Orchestrator → Spawner (in-process)
**Mechanism**: Function parameter on `spawn_for_tasks()`
**Payload**: `TraceContext` (parent context for child span generation)
**Failure**: Cannot fail (same process)

### Spawner → Agent (environment variables)
**Mechanism**: Subprocess environment via `build_correlation_env()`
**Variables**: `TRACEPARENT`, `BERNSTEIN_TRACE_ID`, `BERNSTEIN_SPAN_ID`
**Failure**: If env vars not set, agent simply doesn't propagate traces (graceful degradation — agent still works)

### Agent → Task Server (HTTP)
**Header**: `traceparent: $TRACEPARENT` (value from environment)
**Failure**: If agent doesn't include header, server still processes request — traces just aren't linked

### Orchestrator → Quality Gates (in-process)
**Mechanism**: Function parameter on gate runner
**Payload**: `TraceContext`
**Failure**: Cannot fail (same process)

### Orchestrator → Merge Queue (in-process)
**Mechanism**: Fields on `MergeJob` dataclass
**Payload**: `trace_id: str`, `parent_span_id: str`
**Failure**: Cannot fail (same process)

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Root OTel span | Step 1 | Automatic (OTel SDK) | Span ends when CLI exits |
| Tick child spans | Step 3 | Automatic (context manager) | `__exit__` closes span |
| Agent child spans | Step 5 | Automatic (agent exit) | Agent process exit flushes OTel |
| Gate child spans | Step 9 | Automatic (context manager) | `__exit__` closes span |
| Merge child span | Step 10 | Automatic (context manager) | `__exit__` closes span |
| `CorrelationRecord` in JSONL | Step 11 | N/A (append-only) | Not destroyed; part of audit trail |

No manual cleanup required. All spans are closed by context managers or process exit. The system degrades gracefully — missing trace context simply means spans are unlinked, not that operations fail.

---

## Graceful Degradation Rules

This workflow must **never** cause a task to fail. Tracing is observability, not control flow.

1. If `generate_trace_context()` raises → catch, log warning, continue without trace
2. If `parse_traceparent()` returns `None` → continue without linking
3. If `TRACEPARENT` env var is missing in agent → agent works normally, just no trace linkage
4. If OTel SDK is not installed → all `start_span()` calls are no-ops (existing behavior)
5. If tracing backend is unreachable → OTel SDK buffers/drops spans silently (existing behavior)

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Root trace creation | `conduct()` called | `TraceContext` generated with valid 32-hex trace_id, 16-hex span_id |
| TC-02: traceparent header propagation | Orchestrator claims tasks | HTTP request includes valid `traceparent` header |
| TC-03: Agent env var injection | Spawner launches agent | Agent subprocess env contains `TRACEPARENT`, `BERNSTEIN_TRACE_ID`, `BERNSTEIN_SPAN_ID` |
| TC-04: Env isolation allowlist | `build_filtered_env()` called | Output dict includes `TRACEPARENT` when present in parent env |
| TC-05: Agent → server header | Agent calls `POST /tasks/{id}/complete` | Server receives `traceparent` header matching agent's env |
| TC-06: Server parses incoming traceparent | Request with valid `traceparent` header | `parse_traceparent()` returns valid `TraceContext`; logs include `trace_id` |
| TC-07: Server handles missing traceparent | Request without `traceparent` header | Request processed normally; no error; no trace linkage |
| TC-08: Server handles malformed traceparent | Request with `traceparent: garbage` | `parse_traceparent()` returns `None`; request processed normally |
| TC-09: Quality gate spans | Gate runner called with trace context | Child span created per gate type with correct `parent_span_id` |
| TC-10: Merge job carries trace | `merge_queue.enqueue()` called | `MergeJob` has `trace_id` and `parent_span_id` fields populated |
| TC-11: Correlation record persistence | Agent session finalizes | `trace_correlations.jsonl` contains record with matching trace_id + session_id |
| TC-12: End-to-end trace continuity | Full task lifecycle (create → claim → spawn → complete → gate → merge) | Single trace_id visible across all component spans in tracing backend |
| TC-13: Graceful degradation — no OTel | OTel SDK not installed | All operations succeed; no spans exported; no errors |
| TC-14: Graceful degradation — bad traceparent | Agent sends malformed traceparent | Server processes request; logs warning; no crash |
| TC-15: Conflict resolver inherits trace | Merge conflict detected | Resolver task created with same trace_id as parent task |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `trace_correlation.py` functions (`generate_trace_context`, `build_correlation_env`, `parse_traceparent`) work correctly | Verified: `tests/unit/test_trace_correlation.py` has comprehensive tests | Low — well-tested |
| A2 | `correlation.py` functions (`create_context`, `set_current_context`, `CorrelationFilter`) work correctly | Verified: tests exist | Low — well-tested |
| A3 | All agent adapters use `build_filtered_env()` from `env_isolation.py` | Partially verified: Claude adapter does; need to verify all 17 adapters | Medium — if an adapter bypasses env isolation, trace vars won't reach agent |
| A4 | Agent subprocesses can read environment variables | Verified: agents already read `ANTHROPIC_API_KEY` from env | Low |
| A5 | Agents will include the `traceparent` header if instructed in prompt | Not verified: depends on agent LLM compliance | Medium — agents may omit the header; graceful degradation covers this |
| A6 | `httpx` client used by orchestrator supports custom headers | Verified: standard httpx feature | Low |
| A7 | OTel `start_span()` accepts parent context parameter | Verified: OTel SDK standard feature | Low |
| A8 | `MergeJob` dataclass can be extended with new fields without breaking existing code | Verified: dataclass with keyword args | Low |

---

## Open Questions

1. **Should the CLI print the trace_id?** Useful for operators to grep tracing backend. Recommended: yes, as part of the startup banner.
2. **Should trace_id be stored on the Task object in the task server?** Would enable querying "show me the trace for task X" without parsing logs. Adds a field to the data model.
3. **Should conflict resolver tasks inherit the parent trace_id or start a new trace?** Recommendation: inherit (same logical unit of work), but open to product input.
4. **Should the traceparent be injected into agent prompts as text (for curl headers) AND as env vars?** Recommendation: both — env var for programmatic access, curl header template for LLM-generated HTTP calls.
5. **What is the retention policy for `trace_correlations.jsonl`?** Currently append-only with no rotation.

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created based on codebase discovery | — |
| 2026-04-11 | `trace_correlation.py` functions exist but are completely unused | Documented in spec; Steps 5, 11 wire them |
| 2026-04-11 | `correlation.py` context/filter exist but are completely unused | Documented in spec; Steps 2, 8 wire them |
| 2026-04-11 | `env_isolation.py` `_BASE_ALLOWLIST` does not include trace variables | Documented in spec; Step 6 adds them |
| 2026-04-11 | `tick_telemetry.py` spans are not linked to any parent trace | Documented in spec; Step 3 wires parent context |
| 2026-04-11 | Agent curl templates in spawner prompt have no `traceparent` header | Documented in spec; Step 7 adds the header |
| 2026-04-11 | `MergeJob` dataclass has no trace fields | Documented in spec; Step 10 extends it |

---

## Implementation Priority

Recommended implementation order (each step is independently shippable):

1. **Step 6** (env isolation allowlist) — smallest change, unblocks everything downstream
2. **Step 1 + Step 2** (root trace creation + orchestrator) — establishes the root
3. **Step 5** (spawner env injection) — wires unused `build_correlation_env()`
4. **Step 7** (agent curl template) — enables agent → server trace linkage
5. **Step 4 + Step 8** (HTTP header propagation) — orchestrator ↔ server linkage
6. **Step 9 + Step 10** (gates + merge) — completes the chain
7. **Step 11** (persistence) — wires unused `save_correlation()`
8. **Step 3** (tick telemetry parent linking) — polish; connects tick spans to root

Each step can be deployed independently. Missing steps result in broken trace chains (gaps), not broken functionality.
