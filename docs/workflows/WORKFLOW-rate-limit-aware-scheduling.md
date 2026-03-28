# WORKFLOW: Rate-Limit-Aware Agent Scheduling
**Version**: 0.1
**Date**: 2026-03-28
**Author**: Workflow Architect
**Status**: Draft
**Implements**: Backlog #335c

---

## Overview

When a provider returns HTTP 429 (Too Many Requests) to an agent subprocess, that agent stalls
or dies. Without detection and rotation, multiple agents pile onto the same throttled provider,
multiplying the stall. This workflow covers the full lifecycle: detecting a 429 from agent logs,
marking the provider as throttled, rotating new spawns away from it, spreading existing load
across providers, and auto-recovering when the throttle period expires.

The workflow operates entirely in the orchestrator process. Agents are short-lived subprocesses
that cannot report their own provider state back to the orchestrator — detection is inference from
log content and exit patterns.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator tick | Drives detection, rotation, spreading, and recovery checks |
| TierAwareRouter | Holds per-provider state; selects provider on each spawn |
| RateLimitTracker | New component: tracks throttle state with expiry timestamps |
| CLIAdapter (all adapters) | Spawns agent subprocess; its log is scanned for 429 |
| MetricsCollector | Persists rate-limit events to `.sdd/metrics/` for dashboards |
| Agent subprocess | Receives 429 from provider API; cannot directly report it |

---

## Prerequisites

- At least one provider registered with TierAwareRouter
- Agent log file exists at `.sdd/runtime/{session_id}.log` when agent exits
- Orchestrator has access to the metrics collector instance

---

## Trigger

This workflow is composed of four sub-workflows, each with a distinct trigger:

| Sub-workflow | Trigger |
|---|---|
| A: 429 Detection | Agent session transitions to `dead` status during orchestrator tick |
| B: Throttle-Aware Spawn | `claim_and_spawn_batches()` selects a provider for a new agent |
| C: Throttle Recovery | Orchestrator tick (every poll_interval_s seconds, e.g., every 3s) |
| D: Provider Spreading | `claim_and_spawn_batches()` — runs alongside sub-workflow B |

---

## Sub-Workflow A: 429 Detection and Provider Throttling

### STEP A1: Detect Agent Death
**Actor**: Orchestrator tick (`reap_dead_agents`)
**Action**: Agent process poll returns non-None (process exited). Session status → `dead`.
**Timeout**: None (synchronous poll)
**Input**: `{ session_id: str, pid: int, provider: str | None }`
**Output on SUCCESS**: Session marked `dead` → GO TO STEP A2
**Output on FAILURE**:
  - `FAILURE(no_log_file)`: Log path does not exist → SKIP A2, continue reap

**Observable states**:
  - Operator sees: Agent in `dead` state in `bernstein status`
  - Database: AgentSession.status = "dead"
  - Logs: `[agent_lifecycle] agent {session_id} reaped (pid={pid})`

---

### STEP A2: Scan Agent Log for 429 Patterns
**Actor**: `RateLimitTracker.scan_log_for_429(log_path)`
**Action**: Read last 500 lines of agent log. Search for patterns indicating rate limiting:
  - HTTP 429 response status
  - Strings: "rate limit", "too many requests", "overloaded", "quota exceeded", "rateLimitError"
  - JSON stream-json `result` message with `error` field containing above patterns
**Timeout**: 2s (log I/O)
**Input**: `{ log_path: Path, session_id: str, provider: str | None }`
**Output on SUCCESS (429 found)**: `{ provider: str, pattern_matched: str }` → GO TO STEP A3
**Output on SUCCESS (no 429)**: No action taken → END sub-workflow A
**Output on FAILURE**:
  - `FAILURE(io_error)`: Log unreadable → log warning, skip throttle → END sub-workflow A

**Assumption A1**: When the Claude Code CLI hits a 429 from the Anthropic API, it writes
recognizable error text to its stream-json output before exiting. This must be verified
empirically — if Claude Code retries silently and never surfaces the 429, this detection
mechanism will not fire. Alternative: treat agent stall + death as a 429 signal when
no other error pattern is found.

**Observable states**:
  - Logs: `[rate_limit_tracker] 429 detected in log {session_id}, provider={provider}`

---

### STEP A3: Mark Provider as Throttled
**Actor**: `RateLimitTracker.throttle_provider(provider, duration_s)`
**Action**:
  1. Record `ThrottleState(provider, throttled_until=now+duration_s, trigger_count++)`
  2. Call `TierAwareRouter.set_provider_status(provider, ProviderHealthStatus.RATE_LIMITED)`
  3. Call `MetricsCollector.mark_provider_rate_limited(provider, reset_time=throttled_until)`
  4. Log the throttle event
**Timeout**: Synchronous (no I/O)
**Input**: `{ provider: str, duration_s: int }` — default duration_s: 60s; exponential backoff on repeated throttles: `min(60 * 2^(trigger_count-1), 3600)` (max 1h)
**Output on SUCCESS**: Provider marked throttled, expiry stored → END sub-workflow A
**Output on FAILURE**:
  - `FAILURE(unknown_provider)`: Provider not registered in router → log warning, skip → END

**Observable states**:
  - Operator sees: Provider status = RATE_LIMITED in dashboard
  - Logs: `[rate_limit_tracker] provider {provider} throttled for {duration_s}s (trigger #{trigger_count})`

---

## Sub-Workflow B: Throttle-Aware Provider Selection

### STEP B1: Check Provider Throttle Status Before Spawn
**Actor**: `TierAwareRouter.get_available_providers()` (called from `claim_and_spawn_batches`)
**Action**: Filter candidate providers. A provider is excluded if:
  - `ProviderHealthStatus.RATE_LIMITED` and `throttled_until > now`
  - `ProviderHealthStatus.UNHEALTHY`
  - `ProviderHealthStatus.OFFLINE`
  - `available == False`
**Timeout**: Synchronous
**Input**: Provider registry + current throttle state from `RateLimitTracker`
**Output on SUCCESS (healthy providers remain)**: Filtered provider list → GO TO STEP B2
**Output on FAILURE**:
  - `FAILURE(all_providers_throttled)`: No available providers for required model → GO TO STEP B3

**Observable states**:
  - Logs: `[router] provider {provider} excluded (RATE_LIMITED, throttled until {ts})`

---

### STEP B2: Select Provider with Spreading Score
**Actor**: `TierAwareRouter._calculate_provider_score()` (modified)
**Action**: Score each available provider. Add a spreading penalty to the existing scoring formula:
  ```
  score = health * 0.35 + cost * 0.25 + free_tier * 0.2 + latency * 0.1 + spreading * 0.1
  ```
  Where `spreading = 1.0 - (active_agents_on_provider / max_agents)`.
  This biases selection away from providers already running many agents.
**Timeout**: Synchronous
**Input**: `{ providers: list[ProviderConfig], active_agents_per_provider: dict[str, int] }`
**Output on SUCCESS**: Highest-scoring provider selected → GO TO STEP B4
**Output on FAILURE**: Falls through to standard routing (spreading score = 0 for all)

---

### STEP B3: Handle All-Providers-Throttled Case
**Actor**: `claim_and_spawn_batches` — throttle guard
**Action**:
  1. Do NOT spawn an agent for this batch
  2. Do NOT fail the tasks — they remain `open`
  3. Log the deferral at INFO level (not ERROR — this is expected behavior)
  4. Skip to next batch
**Timeout**: Synchronous
**Output**: Batch deferred → END sub-workflow B for this batch

**Observable states**:
  - Operator sees: Tasks remain `open`, no agents spawned for their role
  - Logs: `[task_lifecycle] all providers throttled for model {model}, deferring batch {batch_ids}`

**NOTE**: If all providers are throttled for more than `orch._config.heartbeat_timeout_s`,
the task should NOT be failed — the throttle will recover and the task will be picked up on
the next tick after recovery. Failing the task here would create duplicate retry chains and
waste quota upon recovery.

---

### STEP B4: Spawn Agent on Selected Provider
**Actor**: `claim_and_spawn_batches` — existing spawn logic
**Action**: Normal spawn path. Record the spawned agent's provider in `AgentSession.provider`.
**Timeout**: Existing spawn timeout
**Output on SUCCESS**: Agent spawned → END sub-workflow B
**Output on FAILURE**: Existing failure handling (backoff, retry)

---

## Sub-Workflow C: Throttle Recovery

### STEP C1: Check Throttle Expiry on Each Tick
**Actor**: `RateLimitTracker.recover_expired_throttles()` — called from orchestrator tick
**Action**: For each throttled provider in `ThrottleState` registry:
  1. If `throttled_until <= now`: clear throttle, mark provider healthy in router
  2. Call `TierAwareRouter.set_provider_status(provider, ProviderHealthStatus.HEALTHY)`
  3. Call `MetricsCollector.mark_provider_healthy(provider)`
  4. Log recovery
**Timing**: Called once per orchestrator tick (every `poll_interval_s`, default 3s)
**Input**: `RateLimitTracker` internal throttle registry
**Output on SUCCESS (provider recovered)**: Provider available for new spawns starting next tick
**Output on SUCCESS (throttle still active)**: No action

**Observable states**:
  - Operator sees: Provider status returns to HEALTHY in dashboard
  - Logs: `[rate_limit_tracker] provider {provider} recovered after throttle`

---

## Sub-Workflow D: Provider Spreading (Proactive)

Provider spreading is a scoring modifier within Sub-Workflow B (Step B2 above). It is not a
separate procedural workflow — it is a parameter in `_calculate_provider_score()`.

**How active agents per provider are counted**: `RateLimitTracker` or the orchestrator maintains
`active_agents_per_provider: dict[str, int]`, incremented when an agent is spawned on a provider
and decremented when the agent transitions to `dead`. This count is passed to the router during
scoring.

**Spreading semantics**: When two providers have equal health/cost scores, the one with fewer
active agents is preferred. This prevents herding — where all agents pile onto the cheapest
provider until it rate-limits, then all rotate to the next cheapest, etc.

---

## State Transitions (Provider)

```
[healthy]
  → (429 detected from agent log)           → [rate_limited]
  → (ProviderHealth consecutive_failures ≥5) → [unhealthy]  (existing behavior)

[rate_limited]
  → (throttled_until ≤ now, C1 recovery)    → [healthy]
  → (429 detected again during throttle)    → [rate_limited] (throttle extended)

[unhealthy]
  → (consecutive_successes ≥3)              → [healthy]  (existing behavior)
```

## State Transitions (Task)

```
[open]
  → (all providers throttled at spawn time)  → [open]  (deferred, NOT failed)
  → (provider available, agent spawns)       → [claimed]

[claimed]
  → (agent gets 429, agent dies, retry)      → [open] [RETRY N]
```

---

## New Component: RateLimitTracker

### Dataclass

```python
@dataclass
class ThrottleState:
    provider: str
    throttled_until: float      # Unix timestamp
    trigger_count: int = 1      # Number of times this provider has been throttled (for backoff)

@dataclass
class ActiveAgentCounts:
    per_provider: dict[str, int]  # provider_name -> count of alive agents
```

### Interface

```python
class RateLimitTracker:
    def scan_log_for_429(self, log_path: Path, session_id: str, provider: str | None) -> bool
    def throttle_provider(self, provider: str, now: float | None = None) -> None
    def recover_expired_throttles(self, router: TierAwareRouter, collector: MetricsCollector) -> list[str]
    def is_throttled(self, provider: str) -> bool
    def get_active_agent_count(self, provider: str) -> int
    def increment_active(self, provider: str) -> None
    def decrement_active(self, provider: str) -> None
    def get_all_throttled(self) -> list[str]
```

### Throttle Duration Formula

```
base_duration_s = 60
duration_s = min(base_duration_s * 2^(trigger_count - 1), 3600)
```

Trigger count is reset to 0 after a successful (non-429) task completion on the provider.

---

## Handoff Contracts

### Orchestrator → RateLimitTracker (on agent death)
**Called by**: `reap_dead_agents` or `process_completed_tasks`
**Method**: `rate_limit_tracker.scan_log_for_429(log_path, session_id, provider)`
**Returns**: `bool` — True if 429 detected (triggers throttle)
**Timeout**: 2s — if log scan exceeds this, return False

### RateLimitTracker → TierAwareRouter (on throttle)
**Method**: `router.update_provider_availability(name, available=False)` or
            a new `router.set_provider_throttled(name, until_ts)` method
**No response expected** (in-memory state mutation)

### Orchestrator tick → RateLimitTracker (recovery check)
**Called by**: Top of each orchestrator tick
**Method**: `rate_limit_tracker.recover_expired_throttles(router, collector)`
**Returns**: `list[str]` — provider names that were recovered

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| ThrottleState entry | A3 | C1 (recovery) | Dict pop on expiry |
| RATE_LIMITED status on provider | A3 | C1 (recovery) | router.set_provider_status(HEALTHY) |
| MetricsCollector rate_limited record | A3 | C1 (recovery) | mark_provider_healthy() |
| active_agent_count increment | B4 | agent death / reap | decrement_active() |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `get_available_providers()` in router.py does NOT exclude `RATE_LIMITED` status — only `UNHEALTHY` and `OFFLINE` are excluded | Critical | Step B1 | Must add `RATE_LIMITED` to exclusion list in router.py |
| RC-2 | `ProviderStatus` in `metrics.py` and `ProviderHealthStatus` in `router.py` are separate enums with the same values — they are not linked. `mark_provider_rate_limited()` in metrics.py does NOT update router state | Critical | Steps A3, B1 | `RateLimitTracker` must update both metrics AND router state on throttle/recovery |
| RC-3 | `ProviderConfig.rate_limit_rpm` is stored but never enforced — no proactive rate budget check before spawning | High | Step B2 | Future work: pre-emptive rate budget check; out of scope for this spec |
| RC-4 | Agent subprocess is opaque — Bernstein does not parse the actual HTTP response code. Detection relies on log text patterns. If Claude Code suppresses 429 details in its stream-json output, detection will miss them | High | Step A2 | Empirically verify by triggering a 429 against a test account; document fallback: treat agent stall-then-death with zero file changes as possible 429 indicator |
| RC-5 | `AgentSession.provider` may be `None` for agents spawned without TierAwareRouter involvement (e.g., when providers.yaml is absent). Without provider attribution, log scanning cannot determine which provider to throttle | Medium | Step A2 | Default to "default" provider name; document that multi-provider rotation requires providers.yaml |
| RC-6 | No spreading modifier currently exists in `_calculate_provider_score()` — active agent counts are not tracked | Medium | Step B2, Sub-workflow D | New `active_agents_per_provider` dict needed in `RateLimitTracker` |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — no rate limit | Agent completes successfully | Log scan finds no 429; provider remains HEALTHY |
| TC-02: 429 in log → throttle | Agent dies, log contains "rate limit exceeded" | Provider marked RATE_LIMITED for 60s; next spawn skips this provider |
| TC-03: Throttle expiry recovery | 60s elapses after throttle | Provider transitions back to HEALTHY; next spawn can use it |
| TC-04: All providers throttled | Both providers hit 429 | Batches deferred (tasks remain open); no spawns; tasks picked up after recovery |
| TC-05: Exponential backoff | Same provider throttled 3 times consecutively | Throttle durations: 60s → 120s → 240s |
| TC-06: Trigger count reset | Provider completes successful task after throttle recovery | trigger_count resets to 0; next throttle starts at 60s |
| TC-07: No provider attribution | Agent spawned without TierAwareRouter (no providers.yaml) | Log scan skipped (no provider to throttle); warning logged |
| TC-08: Log scan I/O error | Agent log unreadable | Warning logged; no throttle; provider remains HEALTHY |
| TC-09: Provider spreading | Two equal-cost providers, one has 4 active agents | Agent spawned on provider with fewer active agents |
| TC-10: Throttle during ongoing task | Provider throttled while 3 agents still running on it | Running agents not killed; only NEW spawns avoid throttled provider |
| TC-11: Task deferred then resumed | Task deferred due to all-throttled; throttle expires | Task spawned normally on next tick after recovery |
| TC-12: 429 detection — no text match | Agent dies, log has no recognizable 429 pattern | No throttle; agent failure handled by existing retry path |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Claude Code CLI logs a recognizable 429 indicator in its stream-json output | NOT verified | Detection misses rate limits; RC-4 mitigation needed |
| A2 | Throttle recovery check adds negligible overhead to tick loop (< 0.5ms) | Not measured | Latency added per tick; use lazy eval if needed |
| A3 | A 60-second base throttle duration is appropriate for Anthropic's actual rate limit windows | Based on typical API behavior | Too short = immediate re-throttle; too long = wasted capacity |
| A4 | `AgentSession.provider` reliably reflects the provider that ran the agent | Verified: set in `claim_and_spawn_batches` at spawn time | Without this, log scan cannot attribute 429 to correct provider |
| A5 | Multiple simultaneous agents on same throttled provider will each die; collective detection is not needed | Design assumption | If only some agents die with 429, partial detection may leave some agents retrying fruitlessly |

## Open Questions

- Should a 429-killed task be immediately retried (as existing retry path does), or should it be held
  back until the throttle clears? Retrying on a throttled provider wastes the retry budget. Consider
  adding a per-task `retry_after` field to prevent premature retry while throttle is active.
- Should the throttle state be persisted to disk (`.sdd/runtime/rate_limits.json`) so that it
  survives an orchestrator restart? Without persistence, a restart clears all throttles and the
  orchestrator may immediately re-trigger 429s on providers that were mid-throttle.
- What is the correct behavior when the ONLY registered provider is throttled? The current design
  defers indefinitely. Should there be a max-deferral time before escalating to operator alert?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-28 | Initial spec created | — |
| 2026-03-28 | RC-1: RATE_LIMITED not excluded in router.get_available_providers() | Documented as critical implementation requirement |
| 2026-03-28 | RC-2: metrics.py and router.py have separate, unlinked provider status enums | RateLimitTracker specified to bridge both |
