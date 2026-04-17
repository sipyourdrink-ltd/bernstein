# WORKFLOW: Cluster Task Stealing for Load Balancing
**Version**: 0.1
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-007

---

## Overview

When a cluster node is overloaded (queue depth exceeds threshold) and another node is idle (available slots exceed threshold), the idle node should steal unclaimed/queued tasks from the overloaded node. This rebalances work across the cluster. The workflow must prevent double-claims via optimistic locking (CAS version checks) and avoid thrashing via cooldown periods.

---

## Actors
| Actor | Role in this workflow |
|---|---|
| Central Server | Hosts task store, evaluates steal policy, executes force-claim resets |
| Donor Node (overloaded) | Worker node with queue_depth > overload_threshold; loses tasks |
| Receiver Node (idle) | Worker node with available_slots >= idle_threshold; gains tasks |
| External Monitor (optional) | Triggers steal evaluation; could be a cron tick on Central or a worker self-report |
| TaskStore | In-memory task state with async lock, CAS versioning, JSONL persistence |
| NodeRegistry | Tracks node status, capacity, heartbeat freshness |
| TaskStealPolicy | Greedy donor-receiver pairing algorithm |
| TaskStealingEngine | Cooldown management, pinned-task filtering, steal history |

---

## Prerequisites
- Cluster mode enabled in `bernstein.yaml` (`cluster.enabled: true`)
- At least 2 nodes registered and ONLINE in NodeRegistry
- Heartbeats current (within `node_timeout_s`) for both donor and receiver
- Central server task store has tasks in CLAIMED or OPEN state
- Steal policy configuration loaded (thresholds, batch size, cooldowns)

---

## Trigger

Steal evaluation is triggered by one of:
1. **Explicit API call**: `POST /cluster/steal` with `queue_depths` payload (external monitor or scheduled tick)
2. **Future**: Periodic tick on Central server evaluating load imbalance automatically
3. **Future**: Worker-initiated pull request ("I'm idle, give me work")

Currently only trigger (1) is implemented. Triggers (2) and (3) are gaps — see Open Questions.

---

## Workflow Tree

### STEP 1: Collect Load Metrics
**Actor**: External Monitor or Central Server tick
**Action**: Gather queue_depth per node from heartbeat data or direct query
**Timeout**: 5s
**Input**: `{ queue_depths: Record<node_id, number> }`
**Output on SUCCESS**: Load snapshot with per-node queue depths -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(timeout)`: Central server unreachable -> retry 1x after 5s -> ABORT (skip this steal cycle, next tick retries)
  - `FAILURE(partial_data)`: Some nodes didn't report -> proceed with available data, log warning

**Observable states during this step**:
  - Customer sees: nothing (internal rebalancing)
  - Operator sees: steal evaluation triggered in logs
  - Database: no change
  - Logs: `[cluster] steal evaluation triggered, nodes_reporting=N`

---

### STEP 2: Evaluate Steal Policy
**Actor**: Central Server (TaskStealPolicy.find_steal_pairs)
**Action**: Identify donor-receiver pairs using greedy matching:
  1. Donors: nodes where `queue_depth > overload_threshold` (default: 5), sorted by excess descending
  2. Receivers: nodes where `available_slots >= idle_threshold` (default: 2), sorted by available slots descending
  3. For each donor, pair with best receiver. Steal count = `min(excess, receiver_slots, max_steal_per_tick)` (default max: 3)
**Timeout**: 1s (pure computation, no IO)
**Input**: `{ queue_depths: Record<node_id, number>, node_registry: NodeRegistry }`
**Output on SUCCESS**: List of `(donor_id, receiver_id, steal_count)` tuples -> GO TO STEP 3
**Output on NO_CANDIDATES**: No donors above threshold or no receivers with slots -> RETURN empty response (no action needed)

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: policy evaluation result in logs
  - Database: no change
  - Logs: `[cluster] steal policy: donors=[alpha(excess=2)], receivers=[beta(slots=4)], pairs=[(alpha,beta,2)]`

---

### STEP 3: Check Cooldowns
**Actor**: Central Server (TaskStealingEngine)
**Action**: For each donor-receiver pair, check if the pair is on cooldown (recently stolen from this donor)
**Timeout**: N/A (in-memory lookup)
**Input**: `{ pairs: List[(donor_id, receiver_id, count)] }`
**Output on SUCCESS**: Filtered pairs with cooldown-blocked pairs removed -> GO TO STEP 4
**Output on ALL_ON_COOLDOWN**: All pairs blocked by cooldown -> RETURN `StealResult.COOLDOWN` (wait for cooldown expiry)

**Observable states during this step**:
  - Logs: `[cluster] cooldown check: pairs_remaining=N, pairs_blocked=M`

**GAP — Cooldown Persistence**: Cooldowns are in-memory only. Server restart clears all cooldowns, allowing immediate re-steal of the same pair. Risk: thrashing after restart. Mitigation: persist cooldowns to `.sdd/runtime/steal_cooldowns.json`.

---

### STEP 4: Select Tasks to Steal from Donor
**Actor**: Central Server (TaskStore query + TaskStealingEngine.select_tasks_to_steal)
**Action**: For each donor in remaining pairs:
  1. Query `store.list_tasks(status="claimed")` to get all claimed tasks
  2. Filter to tasks assigned to the donor node (by `assigned_node` field — **GAP**: field missing, see below)
  3. Exclude pinned tasks (`pinned_node` set and != receiver — **GAP**: field missing on Task model)
  4. Sort by priority (lowest first — steal least important) then by queue_time (oldest first)
  5. Select up to `steal_count` tasks
**Timeout**: 2s
**Input**: `{ donor_id: string, steal_count: number }`
**Output on SUCCESS**: List of `StealableTask` objects -> GO TO STEP 5
**Output on NO_STEALABLE**: All tasks pinned or already terminal -> RETURN `StealResult.NO_CANDIDATES`

**Observable states during this step**:
  - Logs: `[cluster] selected N tasks from donor=alpha: [task_id_1, task_id_2]`

**GAP — No assigned_node Field**: Task model lacks `assigned_node`. Current code falls back to sorting all claimed tasks by version (oldest first), which may steal tasks from any node, not just the donor. Risk: stealing from a node that isn't overloaded. Fix: add `assigned_node: str | None` to Task model, set on claim.

**GAP — No pinned_node Field**: Task model lacks `pinned_node`. TaskStealingEngine supports pinning logic, but Task dataclass doesn't carry the field. Risk: locality-sensitive tasks (e.g., needing local file state) get stolen and fail on the receiver. Fix: add `pinned_node: str | None` to Task model.

---

### STEP 5: Force-Claim Reset (Per Task)
**Actor**: Central Server (TaskStore.force_claim)
**Action**: For each selected task, atomically reset it:
  1. Acquire `_lock` (asyncio.Lock)
  2. Verify task is CLAIMED or IN_PROGRESS (not terminal)
  3. Transition task to OPEN
  4. Clear `claimed_by_session`
  5. Set `priority = 0` (boosted for immediate re-claim)
  6. Increment `version`
  7. Append state change to JSONL log
  8. Release `_lock`
**Timeout**: 1s per task
**Input**: `{ task_id: string }`
**Output on SUCCESS**: Task reset to OPEN with new version -> continue to next task
**Output on FAILURE**:
  - `FAILURE(task_terminal)`: Task already DONE/FAILED/CANCELLED -> skip, log warning, continue with remaining tasks
  - `FAILURE(version_conflict)`: Task version changed during selection -> skip, log, continue (stale selection)
  - `FAILURE(lock_timeout)`: Could not acquire store lock within timeout -> ABORT_PARTIAL (return what was stolen so far)

**Observable states during this step**:
  - Operator sees: task status flip CLAIMED -> OPEN in task list
  - Database (JSONL): `{ task_id, status: "open", version: N+1, force_claimed: true }`
  - Logs: `[cluster] force_claim task_id=abc123: CLAIMED->OPEN (version 5->6, stolen from donor=alpha)`

**GAP — No Original Owner Notification**: When a task is force-claimed, the original claiming agent/orchestrator is not notified. The original node may continue working on the task, producing a conflict when it tries to complete. Risk: wasted compute, potential state corruption. Fix: publish SSE event `task_stolen` with `{ task_id, original_node, new_status }` so the original orchestrator can abort the agent.

---

### STEP 6: Return Steal Response
**Actor**: Central Server
**Action**: Assemble response with all steal actions and total stolen count
**Input**: Accumulated steal results from Step 5
**Output**: `TaskStealResponse { actions: [{ donor_node_id, receiver_node_id, task_ids }], total_stolen: N }`

**Observable states during this step**:
  - Logs: `[cluster] steal complete: total_stolen=2, actions=[(alpha->beta: [abc, def])]`

---

### STEP 7: Receiver Claims Stolen Tasks
**Actor**: Receiver Node (WorkerLoop poll cycle)
**Action**: Normal poll cycle picks up newly-OPEN tasks:
  1. Worker calls `GET /tasks/next/{role}` for each of its configured roles
  2. `claim_next()` pops from priority queue — stolen tasks have priority=0 (highest)
  3. CAS version check on claim (version must match current)
  4. Transition OPEN -> CLAIMED, increment version
  5. Return task to worker
**Timeout**: Worker poll interval (default 10s)
**Input**: `{ role: string }`
**Output on SUCCESS**: Task claimed by receiver -> GO TO STEP 8
**Output on FAILURE**:
  - `FAILURE(version_conflict)`: Another worker claimed it first -> 409, try next task in queue
  - `FAILURE(no_tasks)`: Stolen tasks already claimed by someone else -> normal idle behavior

**Observable states during this step**:
  - Operator sees: task now claimed by receiver node
  - Database (JSONL): `{ task_id, status: "claimed", claimed_by_session: beta_session, version: N+2 }`
  - Logs: `[worker:beta] claimed task_id=abc123 (role=backend)`

**GAP — No Atomic Handoff**: Between Step 5 (force-claim reset to OPEN) and Step 7 (receiver claims), any worker can claim the task. There is no mechanism to reserve the task for the intended receiver. Risk: a third node claims it, defeating the rebalancing intent. Mitigation options:
  - (a) Add `reserved_for_node` field, checked during claim
  - (b) Accept best-effort rebalancing (task goes to whoever claims first — still reduces donor load)

---

### STEP 8: Agent Execution
**Actor**: Receiver Node (AgentSpawner)
**Action**: Spawn CLI agent for the stolen task:
  1. Fork agent process (claude/codex/gemini/etc.) via adapter
  2. Track PID in WorkerLoop active task map
  3. Agent executes task instructions
**Timeout**: Task-level SLA (varies)
**Output on SUCCESS**: Agent produces result -> GO TO STEP 9
**Output on FAILURE**:
  - `FAILURE(agent_crash)`: Process exits non-zero -> `POST /tasks/{id}/fail` with error details
  - `FAILURE(agent_timeout)`: Agent exceeds task SLA -> kill process, `POST /tasks/{id}/fail`

**Observable states during this step**:
  - Customer sees: nothing (internal agent execution)
  - Operator sees: task IN_PROGRESS, agent PID active on receiver node
  - Logs: `[worker:beta] spawned agent pid=12345 for task_id=abc123`

---

### STEP 9: Task Completion
**Actor**: Agent (via task server API)
**Action**: Agent reports completion:
  1. `POST /tasks/{task_id}/complete` with `result_summary`
  2. Store transitions CLAIMED -> DONE, increments version
  3. SSE event published
  4. Plugin hooks fired
**Timeout**: 5s
**Output on SUCCESS**: Task DONE, cluster rebalanced

**Observable states during this step**:
  - Operator sees: task DONE, originally from donor, completed on receiver
  - Database (JSONL): `{ task_id, status: "done", version: N+3, result_summary: "..." }`
  - Logs: `[task_server] task_id=abc123 completed (stolen from alpha, executed on beta)`

---

### ABORT_PARTIAL: Partial Steal Failure
**Triggered by**: Lock timeout in Step 5, or individual task force-claim failures
**Actions**:
  1. Return response with successfully stolen tasks only
  2. Log which tasks failed to steal and why
  3. Failed tasks remain in their current state (no cleanup needed — force-claim is idempotent)
**What operator sees**: Partial steal in logs with details on failures
**Risk**: Low — partial steals still reduce donor load. Next steal cycle handles remaining imbalance.

---

## State Transitions

```
Task states during stealing:

[CLAIMED on donor] -> (force_claim) -> [OPEN, priority=0]
[OPEN, priority=0] -> (receiver claim_next) -> [CLAIMED on receiver]
[CLAIMED on receiver] -> (agent completes) -> [DONE]
[CLAIMED on receiver] -> (agent fails) -> [FAILED]

[CLAIMED on donor] -> (force_claim, task terminal) -> ERROR (skip, no transition)
[OPEN, priority=0] -> (third party claims) -> [CLAIMED on third party] (race condition)
```

---

## Handoff Contracts

### External Monitor -> Central Server (Steal Trigger)
**Endpoint**: `POST /cluster/steal`
**Auth**: `Authorization: Bearer <cluster_jwt>` (scope: `node:admin`)
**Payload**:
```json
{
  "queue_depths": {
    "node_alpha": 7,
    "node_beta": 1
  }
}
```
**Success response**:
```json
{
  "actions": [
    {
      "donor_node_id": "node_alpha",
      "receiver_node_id": "node_beta",
      "task_ids": ["abc123", "def456"]
    }
  ],
  "total_stolen": 2
}
```
**Failure response**:
```json
{
  "detail": "No steal candidates found"
}
```
**Status codes**: 200 (success, even if total_stolen=0), 401 (auth failure), 500 (internal error)
**Timeout**: 10s

### Worker -> Central Server (Claim Stolen Task)
**Endpoint**: `GET /tasks/next/{role}`
**Auth**: `Authorization: Bearer <agent_token>`
**Success response**: Task JSON with `status: "claimed"`, new version
**Failure response**: 404 (no tasks), 409 (version conflict on claim)
**Timeout**: 5s

### Worker -> Central Server (Heartbeat with Capacity)
**Endpoint**: `POST /cluster/nodes/{node_id}/heartbeat`
**Auth**: `Authorization: Bearer <cluster_jwt>` (scope: `node:heartbeat`)
**Payload**:
```json
{
  "capacity": {
    "available_slots": 4,
    "active_agents": 0,
    "total_slots": 6
  }
}
```
**Success response**: 200 OK
**Timeout**: 5s

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Force-claimed task state (OPEN) | Step 5 | Self-healing | Task gets re-claimed or times out; no orphan risk |
| Steal history records | Step 5 | TaskStealingEngine.clear_history | In-memory, lost on restart |
| Cooldown entries | Step 3 | Expiry or restart | In-memory, auto-expire after cooldown_s |

No persistent resources created by stealing — it only modifies existing task state. Cleanup risk is low.

---

## Concurrency and Locking Analysis

### Double-Claim Prevention
- **Mechanism**: asyncio.Lock in TaskStore + CAS version field
- **Scenario**: Two receivers race to claim the same stolen task
- **Resolution**: First claim succeeds (acquires lock, transitions OPEN->CLAIMED, increments version). Second claim sees version mismatch -> 409 Conflict. Client retries with next task.
- **Verified**: test_concurrent_claims_across_workers in test_cluster_e2e.py

### Force-Claim vs Active Agent
- **Scenario**: Donor's agent is actively working on a task when it's force-claimed
- **Risk**: Agent completes and calls `POST /tasks/{id}/complete` on a task now OPEN or claimed by receiver
- **Current behavior**: `complete()` auto-claims OPEN tasks before completing. If receiver already claimed it, donor's complete call gets 409 or succeeds on wrong version.
- **GAP**: No notification to donor agent to abort. Wasted compute.

### Steal-During-Steal
- **Scenario**: Two steal evaluations run concurrently (e.g., two monitors)
- **Resolution**: force_claim is idempotent per task. Both calls try to reset the same tasks. First succeeds, second sees task already OPEN -> either skips (if checking status) or re-resets (no harm, priority stays 0).
- **Risk**: Low. Total_stolen counts may be inaccurate.

---

## Configuration

### Current (hardcoded in steal route)
```python
overload_threshold = 5   # Queue depth above which node is "overloaded"
idle_threshold = 2        # Available slots above which node is "idle"
max_steal_per_tick = 3    # Max tasks stolen per donor-receiver pair per cycle
```

### Recommended (integrate into ClusterConfig in seed.yaml)
```yaml
cluster:
  steal:
    overload_threshold: 5
    idle_threshold: 2
    max_steal_per_tick: 3
    cooldown_s: 30
    enabled: true
```

**GAP**: Policy parameters are hardcoded in the route handler, not configurable via `bernstein.yaml`. Fix: parse `cluster.steal` section in `_parse_cluster_config()` and pass to TaskStealPolicy.

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path steal | Alpha queue=7, Beta slots=4 | 2 tasks stolen from Alpha, claimed by Beta |
| TC-02: No imbalance | All nodes balanced (queue < threshold) | Empty response, total_stolen=0 |
| TC-03: All on cooldown | Recent steal from same pair | StealResult.COOLDOWN, no tasks moved |
| TC-04: Concurrent claims | Two receivers race for same stolen task | One gets 200, other gets 409 |
| TC-05: Task already terminal | Task completed between selection and force-claim | Skip task, continue with others |
| TC-06: Partial failure | 3 tasks selected, 1 force-claim fails | 2 stolen successfully, response includes only successful steals |
| TC-07: Donor agent conflicts | Donor agent completes task during steal | force_claim finds DONE task, skips it |
| TC-08: Single node cluster | Only 1 node registered | No receivers, empty response |
| TC-09: Node offline mid-steal | Donor goes offline during steal | force_claim succeeds (task store is central), receiver can still claim |
| TC-10: Pinned task exclusion | Task has pinned_node=alpha | Task excluded from steal selection (once pinned_node field exists) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | TaskStore is centralized (single server holds all task state) | Verified: in-memory store in server.py | If distributed, force_claim would need distributed lock |
| A2 | queue_depths in steal request accurately reflect current load | Not verified (caller-provided) | Stale data -> steal from wrong node or unnecessary steal |
| A3 | Receiver node is still online when stolen tasks become OPEN | Partially verified (heartbeat freshness check) | Stolen tasks sit OPEN until another node claims them |
| A4 | asyncio.Lock prevents all races within a single server process | Verified: single-process architecture | If multi-process (gunicorn workers), lock is per-process -> double-claims possible |
| A5 | Priority 0 for stolen tasks ensures they are claimed before new tasks | Verified: priority queue uses lower-is-higher ordering | Low risk |
| A6 | force_claim is idempotent (safe to call twice on same task) | Verified: resets to OPEN regardless of current OPEN/CLAIMED state | Low risk |

---

## Open Questions

1. **Who triggers steal evaluation?** Currently requires external `POST /cluster/steal`. Should the central server run a periodic tick (e.g., every 30s) to auto-evaluate? Should idle workers be able to request work?
2. **Should stolen tasks be reserved for the intended receiver?** Current design is best-effort (any worker can claim). Adding `reserved_for_node` field would guarantee receiver gets the task but adds complexity.
3. **How should the donor agent be notified?** When its task is stolen, the agent is still running. Options: SSE event, kill signal via worker PID tracking, or accept wasted compute.
4. **Multi-process deployment**: If the server runs behind gunicorn with multiple workers, the asyncio.Lock is per-process. Is distributed locking (Redis, file lock) needed?
5. **Steal metrics**: What counters/gauges should be exposed? steal_attempts_total, steal_success_total, steal_latency_seconds, rebalance_events_total?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created from code discovery | — |
| 2026-04-08 | Task model lacks `assigned_node` and `pinned_node` fields | Documented as GAP; stealing falls back to version-based selection |
| 2026-04-08 | Policy thresholds hardcoded in route handler | Documented as GAP; recommend seed.yaml integration |
| 2026-04-08 | Cooldowns are in-memory only | Documented as GAP; recommend file persistence |
| 2026-04-08 | No notification to donor when task is stolen | Documented as GAP; recommend SSE event |
| 2026-04-08 | No atomic handoff to intended receiver | Documented as GAP; assess risk vs complexity |
