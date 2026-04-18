# WORKFLOW: Context Collapse with Drain Retry
**Version**: 0.1
**Date**: 2026-04-03
**Author**: Workflow Architect
**Status**: Draft
**Implements**: T493 (context collapse with drain retry)

---

## Overview

When the orchestrator assembles a spawn prompt whose estimated token count exceeds the context-window budget, a multi-round drain loop progressively removes older, lower-priority message groups until the prompt fits.  Each drain round applies the existing 3-stage collapse pipeline (truncate → drop → strip) against a shrinking section list.  If a round still overflows, the controller drains the next lowest-priority group and retries — up to a bounded maximum.  Every round emits structured metrics so the orchestrator can surface degradation instead of silently truncating forever.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Tick pipeline (`tick_pipeline.py`) | Entry point — calls `collapse_prompt_sections()` before spawn |
| Spawn prompt builder (`spawn_prompt.py`) | Assembles named sections, calls `staged_context_collapse()` |
| Context collapse module (`context_collapse.py`) | Executes the 3-stage collapse (truncate, drop, strip) |
| Drain retry controller (NEW) | Wraps collapse in a bounded retry loop with progressive group drain |
| Recovery tracker (NEW) | Records per-round metrics, emits overflow events, surfaces degradation |
| Orchestrator (`orchestrator.py`) | Consumes recovery markers to log/alert on degraded prompts |

---

## Prerequisites

- Named sections have been assembled by `_render_prompt()` or the tick pipeline.
- Each section has a name that maps to a priority via `_SECTION_PRIORITIES` (priority >= 10 is critical).
- A `token_budget` is set (default 50,000 tokens, ~50% of a 100k context window).
- The 3-stage collapse pipeline (`staged_context_collapse`) is operational.

---

## Trigger

**Where**: `spawn_prompt.py:_render_prompt()` (line ~519) and `tick_pipeline.py:collapse_prompt_sections()` (line ~498).

**When**: The estimated token count of assembled prompt sections exceeds `token_budget`.

**How**: Called synchronously during the tick pipeline, before the CLI adapter receives the prompt.  This is a deterministic code path — no LLM involvement.

---

## Workflow Tree

### STEP 1: Estimate prompt token count

**Actor**: Spawn prompt builder / tick pipeline
**Action**: Sum `len(content) // 4` across all named sections.
**Timeout**: N/A (pure computation, < 1ms)
**Input**: `sections: list[tuple[str, str]]`, `token_budget: int`
**Output on SUCCESS**: `total_tokens <= token_budget` → **GO TO STEP 7** (no collapse needed)
**Output on OVERFLOW**: `total_tokens > token_budget` → **GO TO STEP 2**

**Observable states during this step**:
- Orchestrator sees: nothing (fast path, sub-millisecond)
- Database: no change
- Logs: `DEBUG` "Prompt sections within budget: {total_tokens} tokens (limit {budget})"

---

### STEP 2: Initialize drain retry controller

**Actor**: Drain retry controller
**Action**: Create a `DrainRetryState` with:
- `max_rounds: int = 3` (bounded retry limit)
- `current_round: int = 0`
- `sections_snapshot: list[tuple[str, str]]` (original sections, immutable reference)
- `drainable_groups: list[str]` — non-critical section names sorted by ascending priority (lowest priority first = drained first)
- `drain_cursor: int = 0` — index into `drainable_groups` marking the next group to drain
- `round_history: list[DrainRound]` — telemetry for each round

**Group boundary definition**: A "group" is a named section tuple `(name, content)`.  Groups are ordered for drain by `_section_priority(name)` ascending, then by estimated token count descending (drain largest low-priority sections first).

**Minimum retained tail**: Sections with priority >= 10 (`role`, `task`, `instruction`, `signal`) are NEVER drained.  They form the irreducible minimum prompt.

**Timeout**: N/A (data structure init)
**Input**: `sections`, `token_budget`
**Output on SUCCESS**: `DrainRetryState` initialized → **GO TO STEP 3**

**Observable states during this step**:
- Logs: `DEBUG` "Drain retry initialized: {len(drainable_groups)} drainable groups, max {max_rounds} rounds"

---

### STEP 3: Execute collapse round

**Actor**: Context collapse module
**Action**: Call `staged_context_collapse(current_sections, token_budget)`.  This applies:
1. **Truncate**: Proportionally shrink large non-critical sections.
2. **Drop sections**: Remove lowest-priority sections entirely.
3. **Strip metadata**: Remove lesson/recommendation blocks.

**Timeout**: N/A (pure computation, < 10ms for typical prompt sizes)
**Input**: `current_sections: list[tuple[str, str]]`, `token_budget: int`
**Output on SUCCESS**: `CollapseResult.within_budget == True` → **GO TO STEP 6** (record round, done)
**Output on OVERFLOW**: `CollapseResult.within_budget == False` → **GO TO STEP 4** (need deeper drain)

**Observable states during this step**:
- Logs: `INFO` "Collapse stage {stage}: freed {N} tokens from [{section_names}]" (per stage, from existing `_log_steps`)
- Recovery tracker: `DrainRound` created with `{round_number, tokens_before, tokens_after, stages_applied, sections_affected}`

---

### STEP 4: Check retry bounds

**Actor**: Drain retry controller
**Action**: Increment `current_round`.  Check:
- `current_round > max_rounds` → **GO TO ABORT_OVERFLOW** (retries exhausted)
- `drain_cursor >= len(drainable_groups)` → **GO TO ABORT_OVERFLOW** (no more groups to drain)
- Otherwise → **GO TO STEP 5**

**Timeout**: N/A
**Observable states during this step**:
- Logs: `INFO` "Drain round {current_round}/{max_rounds}: still over budget ({compressed_tokens} > {budget}), draining next group"

---

### STEP 5: Drain next group and retry

**Actor**: Drain retry controller
**Action**:
1. Pop the next drainable group at `drain_cursor` from the working section list.
2. Record what was drained: `{group_name, tokens_freed, round_number}`.
3. Advance `drain_cursor`.
4. **GO TO STEP 3** (re-run collapse on the reduced section list).

**Drain ordering** (deterministic, ascending priority then descending size):

| Drain order | Section keywords | Priority | Rationale |
|---|---|---|---|
| 1st | `specialist` | 2 | Specialist descriptions are least critical for task execution |
| 2nd | `heartbeat` | 2 | Heartbeat instructions are infrastructure, not task-relevant |
| 3rd | `recommendation` | 3 | Context recommendations are advisory |
| 4th | `team`, `awareness`, `bulletin` | 3 | Team awareness is nice-to-have |
| 5th | `lesson` | 4 | Lessons improve quality but are not required |
| 6th | `context` | 5 | Rich context from TaskContextBuilder |
| 7th | `predecessor` | 6 | Predecessor outputs inform but are not essential |
| 8th | `project` | 7 | Project context from `.sdd/project.md` |
| NEVER | `role`, `task`, `instruction`, `signal` | 10 | Critical — defines the agent's identity and assignment |

Within the same priority tier, larger sections (by estimated tokens) are drained first.

**Timeout**: N/A
**Observable states during this step**:
- Logs: `INFO` "Drain round {round}: removed group '{group_name}' ({tokens} tokens), {remaining} groups remain"
- Recovery tracker: drain event recorded

---

### STEP 6: Record success and return

**Actor**: Drain retry controller + recovery tracker
**Action**:
1. Finalize `DrainRetryResult`:
   - `sections`: collapsed sections ready for the CLI adapter.
   - `collapse_result`: inner `CollapseResult` from the last successful round.
   - `rounds_taken`: number of drain rounds executed (0 = no collapse needed).
   - `total_tokens_freed`: sum of tokens freed across all rounds.
   - `groups_drained`: list of group names removed during drain.
   - `degraded`: `True` if any groups were drained (prompt is not full-fidelity).
   - `within_budget`: `True`.
2. If `degraded`, emit structured log:
   `WARNING "Context collapse degraded prompt: drained {N} groups in {rounds} rounds ({freed} tokens freed)"`
3. Return to caller.

**Output**: `DrainRetryResult` → prompt is ready for spawn.

**Observable states during this step**:
- Orchestrator sees: `degraded` flag on the result; can surface in status dashboard
- Logs: `INFO` or `WARNING` depending on degradation level
- Metrics: `context_collapse_rounds` counter, `context_collapse_tokens_freed` gauge, `context_collapse_degraded` boolean

---

### ABORT_OVERFLOW: Retry limit exhausted

**Triggered by**: STEP 4 when `current_round > max_rounds` OR `drain_cursor >= len(drainable_groups)`
**Actions** (in order):
1. Build `DrainRetryResult` with:
   - `within_budget`: `False`
   - `degraded`: `True`
   - `overflow_tokens`: `compressed_tokens - token_budget`
   - `rounds_taken`: `current_round`
   - All round history preserved for diagnostics.
2. Emit structured log:
   `ERROR "Context collapse OVERFLOW: {overflow_tokens} tokens over budget after {rounds} rounds; critical sections alone exceed budget"`
3. Emit recovery marker: write `overflow_event` to structured metrics for the orchestrator to surface.
4. Return best-effort collapsed sections (the smallest result achieved).

**What the caller does**: The spawn prompt builder falls through to `PromptCompressor` as a last-resort fallback.  If that also fails, the prompt is sent uncompressed with a warning.  The orchestrator can choose to:
- Log the overflow to the status dashboard.
- Skip spawning this agent and re-queue the task with a larger model (higher context window).
- Record the event for post-run analysis.

**Observable states**:
- Orchestrator sees: overflow event in metrics; agent spawns with a degraded/oversized prompt
- Logs: `ERROR` with full round history
- Metrics: `context_collapse_overflow` counter incremented

---

## State Transitions

```
[sections_assembled] -> (estimate <= budget) -> [within_budget]
[sections_assembled] -> (estimate > budget) -> [draining]
[draining] -> (collapse succeeds, round 1) -> [within_budget]
[draining] -> (collapse overflows, rounds < max) -> [draining] (next round)
[draining] -> (rounds exhausted OR no groups left) -> [overflow]
[overflow] -> (fallback to PromptCompressor) -> [best_effort]
[overflow] -> (PromptCompressor fails) -> [uncompressed_warning]
```

---

## Handoff Contracts

### Tick pipeline → Drain retry controller

**Function**: `collapse_with_drain_retry(sections, token_budget, *, task_ids, max_rounds)`
**Input**:
```python
sections: list[tuple[str, str]]    # Named prompt sections
token_budget: int                   # Max estimated tokens (default 50_000)
task_ids: list[str] | None          # For log context
max_rounds: int                     # Bounded retry limit (default 3)
```
**Success response**:
```python
DrainRetryResult(
    sections=list[tuple[str, str]],  # Ready for CLI adapter
    collapse_result=CollapseResult,   # Inner collapse diagnostics
    rounds_taken=int,                 # 0 = no collapse needed
    total_tokens_freed=int,
    groups_drained=list[str],
    degraded=bool,                    # True if any groups removed
    within_budget=bool,
    overflow_tokens=int,              # 0 if within budget
    round_history=list[DrainRound],
)
```
**Failure response**: This function does not raise — it always returns a best-effort result.  The `within_budget` and `degraded` flags communicate the outcome.

### Drain retry controller → Context collapse module

**Function**: `staged_context_collapse(sections, token_budget)` (existing, unchanged)
**Input**: `sections: list[tuple[str, str]]`, `token_budget: int`
**Output**: `CollapseResult` (existing, unchanged)

### Drain retry controller → Orchestrator (via recovery marker)

**Mechanism**: Structured log + optional file-based metric
**Payload**:
```json
{
    "event": "context_collapse_overflow",
    "task_ids": ["task-1"],
    "rounds_taken": 3,
    "tokens_over_budget": 2500,
    "groups_drained": ["specialist", "heartbeat", "lesson"],
    "timestamp": 1712150400
}
```

---

## Data Structures (NEW)

### DrainRound

```python
@dataclass
class DrainRound:
    """Record of a single drain retry round."""
    round_number: int
    tokens_before: int       # Estimated tokens entering this round
    tokens_after: int        # Estimated tokens after collapse
    tokens_freed: int        # tokens_before - tokens_after
    group_drained: str       # Section name drained before this round ("" for round 1)
    stages_applied: list[str]  # e.g. ["truncate", "drop_sections", "strip_metadata"]
    within_budget: bool
```

### DrainRetryResult

```python
@dataclass(frozen=True)
class DrainRetryResult:
    """Result of the drain retry loop."""
    sections: list[tuple[str, str]]
    collapse_result: CollapseResult
    rounds_taken: int
    total_tokens_freed: int
    groups_drained: list[str]
    degraded: bool
    within_budget: bool
    overflow_tokens: int
    round_history: list[DrainRound]
```

### DrainRetryConfig

```python
@dataclass
class DrainRetryConfig:
    """Policy configuration for drain retry."""
    max_rounds: int = 3
    token_budget: int = 50_000
    min_drain_tokens: int = 500   # Skip draining groups smaller than this
```

---

## Cleanup Inventory

This workflow creates no persistent resources.  All state is ephemeral within a single tick:

| Resource | Created at step | Lifecycle |
|---|---|---|
| `DrainRetryState` | Step 2 | Stack-local; garbage collected after return |
| `DrainRound` entries | Step 3 (each round) | Embedded in result; no cleanup needed |
| Structured log entries | Steps 3-6, ABORT | Written to logger; no cleanup |
| Overflow metric event | ABORT_OVERFLOW | Written to metrics file; persists for post-run analysis |

No rollback is needed because the workflow operates on in-memory copies of the section list.  The original sections are never mutated.

---

## Integration Points

### Where to add the new code

| Component | File | Change |
|---|---|---|
| `DrainRound`, `DrainRetryResult`, `DrainRetryConfig` | `src/bernstein/core/context_collapse.py` | Add dataclasses after existing `CollapseResult` |
| `collapse_with_drain_retry()` | `src/bernstein/core/context_collapse.py` | New public function after `staged_context_collapse()` |
| `collapse_prompt_sections()` | `src/bernstein/core/tick_pipeline.py` | Replace direct call to `staged_context_collapse` with `collapse_with_drain_retry` |
| `_render_prompt()` | `src/bernstein/core/spawn_prompt.py` | Replace direct call to `staged_context_collapse` with `collapse_with_drain_retry` |

### What NOT to change

- The existing `staged_context_collapse()` function is unchanged — the drain retry wraps it.
- The existing `CollapseResult` and `CollapseStep` types are unchanged.
- The `PromptCompressor` fallback path in `spawn_prompt.py` remains as a last resort.
- The `DrainCoordinator` in `drain.py` is **unrelated** (that is graceful shutdown drain, not context drain).

---

## Concurrency & Ordering Considerations

### No concurrency risk within a single tick

The drain retry loop runs synchronously within a single tick.  Each tick handles one batch of tasks.  There is no shared mutable state between ticks — the section list is assembled fresh each time.

### Does not block unrelated work

The drain retry loop is bounded by `max_rounds` (default 3).  Each round is pure computation (< 10ms).  Total worst-case: ~30ms for 3 rounds.  This is negligible compared to the agent spawn time (~1-5 seconds).

### Ordering guarantee

The drain order is deterministic: sections are drained in ascending priority order, then descending size within a priority tier.  Given the same input sections and budget, the same groups are drained in the same order.  This is important for reproducibility and debugging.

---

## Test Cases

Derived directly from the workflow tree — every branch = one test case.

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Within budget, no collapse | Sections total < budget | Returns original sections unchanged, `rounds_taken=0`, `degraded=False` |
| TC-02: Single-round collapse succeeds | Sections total > budget, stage 1 truncation sufficient | Returns collapsed sections, `rounds_taken=1`, `degraded=False` (truncation only, no groups drained) |
| TC-03: Multi-round drain succeeds | Sections total > budget, first collapse overflows, drain 1 group, second collapse fits | `rounds_taken=2`, `degraded=True`, `groups_drained` has 1 entry |
| TC-04: Max rounds exhausted | Sections total >> budget, even after 3 rounds + drains still over | `within_budget=False`, `overflow_tokens > 0`, `rounds_taken=3`, ERROR logged |
| TC-05: All drainable groups exhausted before max rounds | Very few non-critical sections, all drained but still over | `within_budget=False`, `drain_cursor >= len(drainable_groups)` |
| TC-06: Critical sections alone exceed budget | Only priority-10 sections, all non-critical already absent | `within_budget=False`, `rounds_taken=1`, `groups_drained=[]` |
| TC-07: Drain ordering is deterministic | Same sections, same budget, called twice | Identical `groups_drained` order both times |
| TC-08: Small groups skipped (`min_drain_tokens`) | Drainable group has < 500 tokens | Group skipped, cursor advances to next |
| TC-09: Recovery marker emitted on overflow | Overflow after all rounds | Structured log at ERROR level with round history |
| TC-10: Round history tracks per-round metrics | Multi-round drain | Each `DrainRound` has correct `tokens_before`, `tokens_after`, `group_drained` |
| TC-11: Empty sections handled | `sections=[]` | Returns empty result, `within_budget=True`, `rounds_taken=0` |
| TC-12: Degradation flag propagated | Groups drained but within budget | `degraded=True`, `within_budget=True` |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `_estimate_tokens` (4 chars/token) is accurate enough for budget decisions | Heuristic used throughout codebase (context_collapse.py:109, context_compression.py:603) | Over/under-estimation causes premature or late drain; bounded retry mitigates |
| A2 | Section priority table in `_SECTION_PRIORITIES` is complete for all section names emitted by `_render_prompt` | Verified: both files share the same keyword-based priority lookup | New section names without priority keywords default to 5; may drain unexpectedly |
| A3 | The `PromptCompressor` fallback in `spawn_prompt.py` can handle the case when drain retry returns `within_budget=False` | Verified: existing fallback path at spawn_prompt.py:556 | If PromptCompressor also fails, uncompressed prompt is sent (existing behavior) |
| A4 | 3 drain rounds is sufficient for typical prompt sizes | Not verified — needs empirical data from production runs | If insufficient, increase `max_rounds` in config; log data will show |
| A5 | Draining groups in priority order produces the best trade-off between prompt quality and budget compliance | Design assumption | Alternative: drain by token count regardless of priority — would save more tokens but lose higher-value context |
| A6 | Pure computation time for 3 collapse rounds is < 50ms | Estimated from single-round profiling | If sections are very large (> 200k chars), could be slower; still sub-second |

---

## Open Questions

1. **Should the orchestrator re-route to a larger-context model when overflow is detected?**
   The `router.py` already has `_requires_large_context()`.  If drain retry overflows, the orchestrator could re-queue with a model that has a larger context window.  This is a policy decision for the orchestrator, not the collapse module.

2. **Should `min_drain_tokens` be configurable per section type?**
   Currently proposed as a flat threshold.  Some sections (e.g., single-line metadata) are too small to meaningfully drain.

3. **Should drain retry emit file-based metrics or only structured logs?**
   File-based metrics (`.sdd/metrics/context_drain_*.jsonl`) would enable post-run dashboards.  Structured logs are simpler and sufficient for alerting.  Recommend starting with structured logs and adding file metrics if needed.

4. **Should the `degraded` flag be surfaced to the spawned agent?**
   If the agent knows its context was truncated, it could compensate (e.g., ask for more context via signals).  But this adds complexity to agent prompt parsing.

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-03 | Initial spec created based on code audit of context_collapse.py, tick_pipeline.py, spawn_prompt.py | — |
| 2026-04-03 | Existing code has NO retry loop — collapse runs once, logs warning if still over budget, continues with oversized prompt | Gap identified; this spec addresses it |
| 2026-04-03 | `spawn_prompt.py:_render_prompt()` calls `staged_context_collapse` directly AND `tick_pipeline.py:collapse_prompt_sections()` also calls it — two separate integration points that need the same drain retry wrapper | Both integration points documented in Integration Points table |
| 2026-04-03 | `auto_compact.py` has a circuit breaker pattern (CLOSED/OPEN/HALF_OPEN) that could inform drain retry backoff | Pattern noted; not directly applicable since drain retry is bounded by `max_rounds` (simpler than circuit breaker for a synchronous loop) |
| 2026-04-17 | `auto_compact.py` and `claude_auto_compact.py` consolidated into `compaction_pipeline.py` + `token_monitor.AutoCompactCircuitBreaker` (audit-062) | Dead modules removed; only the live compactor remains |
| 2026-04-03 | `drain.py` (DrainCoordinator) is graceful shutdown drain — completely unrelated to context drain despite sharing the word "drain" | Naming distinction documented to prevent confusion |
