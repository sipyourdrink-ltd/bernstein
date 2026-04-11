# WORKFLOW: Speculative Execution for Branching Task Graphs
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-192 — Speculative execution for branching task graphs

---

## Overview

When a workflow DAG contains conditional branches (e.g., "if tests fail, run fix-bugs;
if tests pass, run deploy"), the orchestrator currently waits for the condition to
resolve before spawning agents on downstream branches. Speculative execution removes
this latency by spawning agents on *both* branches in parallel before the condition
resolves. When the upstream task completes and the condition evaluates, the winning
branch keeps its work and the losing branch is discarded — its agent killed, worktree
deleted, and tasks cancelled.

This trades compute cost (running agents that may be discarded) for latency reduction
on critical paths. It is opt-in per workflow DSL node, bounded by a configurable
concurrency budget, and builds on three existing systems:
1. `workflow_dsl.py` conditional edges (`DAGEdge.condition` + `DAGExecutor`)
2. `warm_pool.py` + `prepare_speculative_warm_pool()` (pre-warming infrastructure)
3. `worktree.py` `WorktreeManager.cleanup()` (isolated branch deletion)

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator (`orchestrator.py`) | Tick loop: identifies speculative candidates, enforces budget, triggers discard |
| DAG Executor (`workflow_dsl.py`) | Evaluates conditional edges, identifies branch groups, determines winners |
| Spawner (`spawner.py`) | Claims and spawns speculative agents in isolated worktrees |
| Warm Pool (`warm_pool.py`) | Pre-creates worktree capacity for speculative branches |
| WorktreeManager (`worktree.py`) | Creates and destroys agent worktrees |
| Task Server (`routes/tasks.py`) | Persists task state transitions (OPEN -> CLAIMED -> IN_PROGRESS -> CANCELLED) |
| Merge Queue (`merge_queue.py`) | Serializes merge decisions; only merges winning branch |
| Agent process (CLI adapter) | Executes work in worktree; may be killed mid-execution |

---

## Prerequisites

- Workflow uses DAG mode (`workflow_dsl.py`) with conditional edges
- At least one conditional branch group exists (two or more nodes sharing an upstream
  dependency with mutually exclusive or overlapping conditions)
- `speculative_execution.enabled: true` in `bernstein.yaml` (default: false)
- Speculative concurrency budget not exhausted (`speculative_execution.max_concurrent`)
- Warm pool has capacity or can create new worktrees

---

## Trigger

Speculative execution is triggered during the orchestrator's normal tick cycle when:
1. A conditional branch group's upstream task is IN_PROGRESS (not yet terminal)
2. The downstream nodes in the branch group are OPEN and blocked by that upstream task
3. The speculative concurrency budget has remaining capacity
4. The branch group is annotated with `speculative: true` in the workflow DSL

---

## DSL Extension

The workflow DSL gains a `speculative` flag on nodes within conditional branch groups:

```yaml
nodes:
  run-tests:
    phase: verify
    role: qa
    depends_on: [build-api]

  fix-bugs:
    phase: implement
    role: backend
    speculative: true          # NEW: spawn before run-tests completes
    depends_on:
      - source: run-tests
        condition: "status == 'failed'"

  deploy:
    phase: merge
    role: manager
    speculative: true          # NEW: spawn before run-tests completes
    depends_on:
      - source: run-tests
        condition: "status == 'done'"
```

Configuration in `bernstein.yaml`:

```yaml
speculative_execution:
  enabled: false               # opt-in
  max_concurrent: 4            # max speculative agents running at once
  max_branch_depth: 1          # how many levels deep to speculate (1 = immediate branches only)
  cost_multiplier_alert: 2.0   # alert if speculative cost exceeds N× normal cost
  discard_timeout_seconds: 30  # max time to wait for agent graceful shutdown before SIGKILL
```

---

## Workflow Tree

### STEP 1: Identify Speculative Branch Groups
**Actor**: DAG Executor
**Action**: During each normal tick, scan the DAG for conditional branch groups:
  1. For each node with `speculative: true`, find its conditional incoming edges
  2. Group nodes that share the same upstream source into a *branch group*
  3. Filter: only groups where the upstream task is IN_PROGRESS (not terminal)
  4. Filter: only groups where no downstream node has already been claimed/spawned
  5. Filter: only groups within `max_branch_depth` of the upstream task
  6. Return list of `SpeculativeBranchGroup` objects
**Timeout**: <1s (in-memory graph traversal)
**Input**: `{ dag: WorkflowDAG, tasks: dict[str, Task] }`
**Output on SUCCESS**: `list[SpeculativeBranchGroup]` -> GO TO STEP 2
**Output on FAILURE**: Not possible (pure graph computation)

**Data structure**:
```python
@dataclass
class SpeculativeBranchGroup:
    group_id: str                    # deterministic hash of upstream + branch node IDs
    upstream_task_id: str            # the IN_PROGRESS task whose result is unknown
    upstream_node_id: str            # DAG node ID of the upstream
    branches: list[SpeculativeBranch]

@dataclass
class SpeculativeBranch:
    node_id: str                     # DAG node ID
    condition: ConditionExpr         # guard predicate on upstream result
    role: str
    estimated_minutes: int
```

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: nothing (graph scan is silent unless branches found)
  - Database: no change
  - Logs: `[speculative] found {N} branch groups eligible for speculation`

---

### STEP 2: Check Speculative Budget
**Actor**: Orchestrator
**Action**: Enforce the concurrency budget before spawning:
  1. Count currently running speculative agents (tasks with `metadata.speculative = true` in CLAIMED or IN_PROGRESS status)
  2. For each branch group, compute how many new agents would be spawned (number of branches minus zero — all branches are speculative)
  3. If `current_speculative + new_speculative > max_concurrent`, prioritize by:
     a. Critical path membership (prefer branches on the critical path)
     b. Upstream task progress (prefer branches whose upstream is closer to completion)
     c. Estimated cost (prefer cheaper branches)
  4. Trim branch groups to fit within budget
**Timeout**: <1s (counter check)
**Input**: `{ branch_groups: list[SpeculativeBranchGroup], budget: SpeculativeConfig }`
**Output on SUCCESS**: `list[SpeculativeBranchGroup]` (trimmed to budget) -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(budget_exhausted)`: All slots taken -> [recovery: skip speculation this tick, log info, retry next tick]

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: nothing
  - Database: no change
  - Logs: `[speculative] budget check: {current}/{max} slots used, {N} groups approved`

---

### STEP 3: Spawn Speculative Agents
**Actor**: Spawner
**Action**: For each approved branch group, spawn agents for ALL branches:
  1. For each branch in the group:
     a. Create a Task from the DAGNode template via `DAGExecutor.create_task()`
     b. Set `task.metadata["speculative"] = True`
     c. Set `task.metadata["branch_group_id"] = group.group_id`
     d. Set `task.metadata["upstream_task_id"] = group.upstream_task_id`
     e. Set `task.metadata["branch_condition"] = branch.condition.raw`
     f. POST task to task server with status OPEN
     g. Claim the task (POST /tasks/{id}/claim)
     h. Create worktree via `WorktreeManager.create(session_id)`
     i. Render agent prompt — include speculative context:
        "You are working speculatively. Your work may be discarded if the
        upstream task resolves against your branch condition. Work normally
        but commit frequently so partial progress can be recovered if needed."
     j. Launch CLI adapter subprocess in the worktree
  2. Record spawn metadata in `.sdd/runtime/speculative/{group_id}.json`:
     ```json
     {
       "group_id": "abc123",
       "upstream_task_id": "run-tests-fa3e12b0",
       "upstream_node_id": "run-tests",
       "branches": [
         {
           "node_id": "fix-bugs",
           "task_id": "fix-bugs-9c2a4b01",
           "session_id": "agent-fix-bugs-9c2a4b01",
           "condition": "status == 'failed'",
           "spawned_at": 1712851200.0
         },
         {
           "node_id": "deploy",
           "task_id": "deploy-7e1d3f02",
           "session_id": "agent-deploy-7e1d3f02",
           "condition": "status == 'done'",
           "spawned_at": 1712851200.5
         }
       ],
       "created_at": 1712851200.0,
       "resolved": false
     }
     ```
**Timeout**: 60s per branch (worktree creation + agent launch)
**Input**: `{ branch_groups: list[SpeculativeBranchGroup] }`
**Output on SUCCESS**: All branches spawned -> GO TO STEP 4 (on next tick)
**Output on FAILURE**:
  - `FAILURE(worktree_create_failed)`: Disk full or git error -> [recovery: skip this branch, log error, continue with remaining branches; partial speculation is acceptable]
  - `FAILURE(spawn_failed)`: Adapter crash on launch -> [recovery: fail the speculative task, log error; other branches continue]
  - `FAILURE(task_claim_conflict)`: Another tick claimed the task -> [recovery: skip, already handled]

**Observable states during this step**:
  - Customer sees: nothing (speculative work is invisible until resolved)
  - Operator sees: agent sessions appear in TUI with `[speculative]` badge
  - Database: tasks created with `metadata.speculative = true`, status CLAIMED then IN_PROGRESS
  - Logs: `[speculative] spawned {N} agents for branch group {group_id}: {branch_node_ids}`

---

### STEP 4: Monitor and Resolve Branch Group
**Actor**: Orchestrator (each tick)
**Action**: On every tick, check each active speculative branch group:
  1. Read `.sdd/runtime/speculative/{group_id}.json`
  2. Look up the upstream task status
  3. If upstream task is still IN_PROGRESS: do nothing, continue monitoring
  4. If upstream task reached a terminal status (DONE, FAILED, CANCELLED):
     a. For each branch in the group, evaluate the branch condition against the
        upstream task's result using `ConditionExpr.evaluate(build_condition_context(upstream_task))`
     b. Classify each branch as WINNER (condition true) or LOSER (condition false)
     c. If exactly one WINNER: -> GO TO STEP 5 (resolve)
     d. If zero WINNERS: -> GO TO STEP 6 (all branches lose — upstream failed unexpectedly)
     e. If multiple WINNERS: -> GO TO STEP 5 with first winner by priority; STEP 6 others
        (overlapping conditions — warn, this is likely a DSL authoring error)
**Timeout**: <1s per group (condition evaluation)
**Input**: `{ speculative_groups: list[SpeculativeGroupState] }`
**Output on SUCCESS**: Classification of winners/losers -> GO TO STEP 5 or STEP 6
**Output on FAILURE**:
  - `FAILURE(condition_eval_error)`: ConditionExpr raises -> [recovery: treat branch as LOSER, log error with condition expression]
  - `FAILURE(upstream_orphaned)`: Upstream task disappeared from task store -> [recovery: discard all branches (STEP 6), log critical warning]

**Observable states during this step**:
  - Customer sees: nothing (resolution happens between ticks)
  - Operator sees: speculative group status in `.sdd/runtime/speculative/` files
  - Database: no change yet (classification is in-memory)
  - Logs: `[speculative] group {group_id} resolved: upstream={status}, winners={winner_ids}, losers={loser_ids}`

---

### STEP 5: Promote Winning Branch
**Actor**: Orchestrator + Task Lifecycle
**Action**: The winning branch's speculative work becomes real:
  1. Update the winning task: remove `metadata.speculative` flag (or set to `false`)
  2. The winning agent continues running normally — no interruption
  3. The winning agent's worktree proceeds through normal completion flow:
     - Agent finishes and calls POST /tasks/{id}/complete
     - Quality gates run
     - Janitor verifies
     - Merge queue merges worktree branch
  4. Update `.sdd/runtime/speculative/{group_id}.json`: set `resolved: true`, `winner: {node_id}`
  5. Trigger STEP 6 for all LOSER branches in the same group
  6. Emit metric: `speculative.resolution` with labels `{group_id, winner_node, latency_saved_ms}`
     - `latency_saved_ms` = estimated_minutes of winning branch × 60000 (work was done in parallel)
**Timeout**: 5s (metadata updates)
**Input**: `{ winner_task_id: str, winner_session_id: str, group: SpeculativeGroupState }`
**Output on SUCCESS**: Winner promoted, losers queued for discard -> STEP 6 for losers
**Output on FAILURE**:
  - `FAILURE(winner_already_completed)`: Agent finished between classification and promotion -> [recovery: proceed normally, winner promotion is a no-op if already completed]
  - `FAILURE(winner_already_failed)`: Agent crashed between classification and promotion -> [recovery: re-evaluate — if another branch can be winner, promote it; else treat as zero-winner case]

**Observable states during this step**:
  - Customer sees: nothing (promotion is invisible)
  - Operator sees: TUI removes `[speculative]` badge from winning agent
  - Database: task metadata updated (speculative flag removed)
  - Logs: `[speculative] promoted winner: task={task_id}, node={node_id}, group={group_id}`

---

### STEP 6: Discard Losing Branches (ABORT_CLEANUP)
**Actor**: Orchestrator + Spawner + WorktreeManager
**Action**: Kill losing agents and clean up their resources:
  1. For each LOSER branch:
     a. Send SHUTDOWN signal to agent: write `.sdd/runtime/signals/{session_id}/SHUTDOWN`
     b. Wait up to `discard_timeout_seconds` (default 30s) for agent process to exit
     c. If agent hasn't exited, send SIGTERM to the agent process (via PID file)
     d. Wait 5s for SIGTERM
     e. If still running, SIGKILL
     f. Transition task to CANCELLED: POST /tasks/{id}/cancel with
        `{"reason": "speculative_branch_discarded", "branch_group_id": "...", "winning_node": "..."}`
     g. Delete the agent's worktree: `WorktreeManager.cleanup(session_id)`
        - Removes `.sdd/worktrees/{session_id}/`
        - Deletes git branch `agent/{session_id}`
        - Removes lock file
     h. Release warm pool slot if one was claimed
     i. Record cost of discarded work in `.sdd/metrics/speculative_waste.jsonl`:
        ```json
        {"group_id": "abc123", "discarded_task_id": "fix-bugs-9c2a4b01",
         "discarded_node": "fix-bugs", "cost_usd": 0.0180,
         "duration_seconds": 42, "reason": "branch_condition_false"}
        ```
  2. After all losers cleaned up, update `.sdd/runtime/speculative/{group_id}.json`:
     set `resolved: true`, `discarded: [list of discarded node IDs]`
  3. Emit metric: `speculative.discard` with labels `{group_id, discarded_nodes, total_waste_usd}`
**Timeout**: `discard_timeout_seconds + 10s` per branch (signal + kill + cleanup)
**Input**: `{ loser_branches: list[SpeculativeBranch], group: SpeculativeGroupState }`
**Output on SUCCESS**: All losers cancelled and cleaned up -> DONE
**Output on FAILURE**:
  - `FAILURE(agent_unkillable)`: Process doesn't respond to SIGKILL -> [recovery: log critical, mark task ORPHANED, let stale agent reaper handle it on next tick]
  - `FAILURE(worktree_cleanup_failed)`: Git worktree remove fails -> [recovery: add to orphan cleanup queue, `cleanup_all_stale()` will catch it at next startup]
  - `FAILURE(task_cancel_rejected)`: Task already in terminal state -> [recovery: no-op, task was already handled]

**Observable states during this step**:
  - Customer sees: nothing (discarded work was never visible)
  - Operator sees: agents disappear from TUI; speculative group marked resolved
  - Database: tasks transition to CANCELLED with `terminal_reason: "speculative_branch_discarded"`
  - Logs: `[speculative] discarded {N} branches for group {group_id}: {discarded_node_ids}, waste=${total_cost:.4f}`

---

### STEP 7: Zero-Winner Resolution
**Actor**: Orchestrator
**Action**: When the upstream task completes but NO branch condition is satisfied:
  1. This means the upstream result was unexpected — none of the speculative branches match
  2. Discard ALL branches (STEP 6 for all)
  3. Log a warning: the workflow DSL has non-exhaustive conditions
  4. The DAG Executor's normal `ready_nodes()` logic handles what happens next:
     - If the upstream failed and has a retry policy, it retries
     - If no retry, downstream nodes remain blocked forever (existing behavior)
  5. Emit metric: `speculative.zero_winner` with labels `{group_id, upstream_status}`
**Input**: `{ all_branches: list[SpeculativeBranch], group: SpeculativeGroupState, upstream_task: Task }`
**Output on SUCCESS**: All branches discarded, warning logged -> DONE
**Output on FAILURE**: Same as STEP 6 failures

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: all speculative agents for this group disappear
  - Database: all speculative tasks CANCELLED
  - Logs: `[speculative] WARNING: zero winners for group {group_id} — upstream status={status} matched no branch conditions. Check workflow DSL for exhaustive conditions.`

---

## State Transitions

```
Speculative Branch Group:
[unresolved] -> (upstream completes, 1 winner)    -> [resolved_with_winner]
[unresolved] -> (upstream completes, 0 winners)   -> [resolved_zero_winners]
[unresolved] -> (upstream completes, N>1 winners)  -> [resolved_multi_winner] (warn, pick first)
[unresolved] -> (budget exceeded, tick skips)      -> [unresolved] (retry next tick)

Speculative Task:
[OPEN] -> (step 3: spawned) -> [CLAIMED] -> [IN_PROGRESS]
[IN_PROGRESS] -> (step 5: promoted as winner) -> [IN_PROGRESS] (continues normally)
[IN_PROGRESS] -> (step 5: normal completion) -> [DONE] -> quality gates -> [CLOSED]
[IN_PROGRESS] -> (step 6: discarded as loser) -> [CANCELLED]
[IN_PROGRESS] -> (agent crashes) -> [FAILED] -> (if speculative, cancel instead of retry)
```

---

## Handoff Contracts

### Orchestrator -> Spawner (speculative spawn)
**Endpoint**: Internal method call `spawner.spawn_for_tasks(tasks, speculative=True)`
**Payload**:
```python
{
    "tasks": list[Task],       # tasks with metadata.speculative = True
    "speculative": True,       # flag to skip normal dependency check
}
```
**Contract**: Spawner must not filter out speculative tasks for unresolved dependencies.
Speculative tasks bypass the normal `all(dep in done_ids for dep in t.depends_on)` filter.

### Orchestrator -> Task Server (cancel speculative)
**Endpoint**: `POST /tasks/{id}/cancel`
**Payload**:
```json
{
    "reason": "speculative_branch_discarded",
    "branch_group_id": "string",
    "winning_node": "string"
}
```
**Success response**: `{"ok": true, "task": {...}}`
**Failure response**: `{"ok": false, "error": "string", "code": "INVALID_TRANSITION"}`
**Timeout**: 5s

### Orchestrator -> Agent (shutdown signal)
**Endpoint**: File write `.sdd/runtime/signals/{session_id}/SHUTDOWN`
**Payload**: `"speculative_branch_discarded\n"`
**Success response**: Agent exits within `discard_timeout_seconds`
**Failure response**: Agent doesn't exit -> escalate to SIGTERM/SIGKILL
**Timeout**: `discard_timeout_seconds` (default 30s)

### DAG Executor -> Orchestrator (branch group identification)
**Payload**:
```python
{
    "branch_groups": list[SpeculativeBranchGroup],
    # each group contains upstream info + list of branches with conditions
}
```
**Contract**: Groups are deterministic given the same DAG + task state. Same input = same output.

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Speculative Task (in task server) | Step 3 | Step 6 | POST /tasks/{id}/cancel |
| Agent worktree (`.sdd/worktrees/{session_id}`) | Step 3 | Step 6 | `WorktreeManager.cleanup(session_id)` |
| Git branch (`agent/{session_id}`) | Step 3 | Step 6 | `git branch -D` (inside WorktreeManager.cleanup) |
| Agent process (OS process) | Step 3 | Step 6 | SHUTDOWN signal -> SIGTERM -> SIGKILL |
| Warm pool slot | Step 3 | Step 6 | `warm_pool.release_slot(slot_id)` |
| Speculative group state file | Step 3 | Never (retained for metrics) | Archived by janitor after 7d |
| Speculative waste metric entry | Step 6 | Never (append-only JSONL) | Rolled by metric rotation |
| Agent JWT token | Step 3 | Step 6 | Token expires (short TTL); revoked on cleanup |
| Agent log file | Step 3 | Step 6 | Deleted with worktree cleanup |
| Lock file (`.sdd/worktrees/{session_id}.lock`) | Step 3 | Step 6 | Removed by WorktreeManager.cleanup |

---

## Interaction with Existing Systems

### Warm Pool (`prepare_speculative_warm_pool`)
The existing warm pool pre-warming in `task_lifecycle.py:211` already identifies tasks
that are one dependency away from being ready. Speculative execution extends this:
- Warm pool continues to pre-create worktree capacity (infrastructure only)
- Speculative execution goes further: actually claims tasks and spawns agents
- The warm pool's `spec-{task_id}` slots can be consumed by speculative spawns

### Dependency Filter Bypass
The orchestrator's dependency filter (`orchestrator.py` ~line 990) blocks tasks whose
`depends_on` includes non-DONE tasks. Speculative tasks must bypass this filter.
The bypass is keyed on `task.metadata.get("speculative") == True` — the spawner
skips the dependency check for these tasks.

### Merge Queue
The merge queue (`merge_queue.py`) serializes branch merges. Speculative tasks are
excluded from the merge queue until promoted (STEP 5). After promotion, the winning
task enters the normal merge flow. Losing tasks never reach the merge queue because
they are cancelled before completion.

### Retry Logic
The orchestrator's `maybe_retry_task()` creates escalated retry tasks for failed tasks.
Speculative tasks that fail should NOT be retried — they should be cancelled (since
the branch resolution will determine their fate). The retry logic checks
`task.metadata.get("speculative")` and skips retry for speculative tasks.

### Cost Tracking
Speculative waste is tracked separately in `.sdd/metrics/speculative_waste.jsonl`.
The cost anomaly detector (`cost_anomaly.py`) should be aware that speculative runs
produce expected "waste" — the `cost_multiplier_alert` threshold controls when this
waste triggers an alert.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | No `/tasks/{id}/cancel` endpoint exists — only `/complete` and `/fail` | Critical | Step 6, Handoff Contracts | Must add `POST /tasks/{id}/cancel` route to `routes/tasks.py` with CANCELLED transition |
| RC-2 | `DAGExecutor.ready_nodes()` skips nodes with non-failed existing tasks — speculative tasks would be IN_PROGRESS and thus skipped | High | Step 1, Step 4 | `ready_nodes()` must be extended or a separate `speculative_candidates()` method added that ignores the "already has active task" check for speculative scanning |
| RC-3 | `DAGNode` has no `speculative` field — the DSL extension is new | Medium | DSL Extension | Add `speculative: bool = False` to `DAGNode` dataclass and YAML parser |
| RC-4 | Task.metadata is `dict[str, Any]` — no typed speculative fields | Low | Step 3 | Acceptable for initial implementation; can introduce `SpeculativeMetadata` TypedDict later |
| RC-5 | `prepare_speculative_warm_pool` only handles tasks one dependency away with exactly 1 unresolved edge — doesn't consider conditional edge semantics | Medium | Step 1, Warm Pool interaction | The warm pool heuristic works as-is for pre-warming but doesn't understand branch groups. Speculative execution handles branch group logic separately. |
| RC-6 | `WorktreeManager.cleanup()` uses `--force` on `git worktree remove` — agent may have uncommitted changes that are lost | Low | Step 6 | Acceptable: losing branch work is discarded by design. Spec notes that agents should commit frequently for potential recovery. |
| RC-7 | No `CANCELLED` status exists in `TaskStatus` enum today — only DONE, FAILED, BLOCKED, etc. | Critical | Step 6, State Transitions | Must verify: if `TaskStatus.CANCELLED` exists, confirm it's in TERMINAL_STATUSES. If not, add it. |
| RC-8 | The SHUTDOWN signal file mechanism exists but agents poll it every 60s — up to 60s delay before agent notices | Medium | Step 6 | `discard_timeout_seconds` default of 30s may be too short. Either reduce agent poll interval for speculative agents or rely on SIGTERM fallback. |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — one winner | Upstream succeeds, deploy branch wins | Deploy agent promoted, fix-bugs agent cancelled and cleaned up |
| TC-02: Happy path — other winner | Upstream fails, fix-bugs branch wins | Fix-bugs agent promoted, deploy agent cancelled and cleaned up |
| TC-03: Zero winners | Upstream cancelled (neither done nor failed matches) | All speculative agents cancelled, warning logged |
| TC-04: Multiple winners (DSL error) | Overlapping conditions both true | First by priority promoted, others cancelled, warning logged |
| TC-05: Budget exhausted | 4 speculative agents running, new group found | New group skipped, retried next tick |
| TC-06: Agent finishes before resolution | Speculative agent completes before upstream | Task in DONE state; promotion is no-op, loser still cancelled |
| TC-07: Agent crashes during speculation | Speculative agent process dies | Task marked FAILED, not retried (speculative), cleaned up on resolution |
| TC-08: Worktree creation fails | Disk full during speculative spawn | Branch skipped, other branches proceed, error logged |
| TC-09: Upstream completes instantly | Upstream task finishes same tick as speculation starts | Resolution runs immediately, minimal waste |
| TC-10: Nested speculation (depth > 1) | Branch group downstream of another speculative branch | Rejected if `max_branch_depth: 1`; allowed if depth increased |
| TC-11: Concurrent branch groups | Two independent branch groups eligible same tick | Both groups spawned within budget |
| TC-12: Speculative disabled | `speculative_execution.enabled: false` | No speculative spawning, normal DAG resolution |
| TC-13: Idempotent resolution | Resolution triggered twice for same group | Second resolution is no-op (group already resolved) |
| TC-14: Cost tracking | Losing branch discarded after 2 minutes | `speculative_waste.jsonl` entry with cost and duration |
| TC-15: Signal timeout + SIGKILL | Agent ignores SHUTDOWN for 35s | SIGTERM at 30s, SIGKILL at 35s, cleanup proceeds |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Conditional branches are mutually exclusive in practice (at most one winner) | Not enforced — DSL allows overlapping conditions | Multiple winners produce a warning and first-by-priority wins; non-fatal but suboptimal |
| A2 | Speculative agents produce no externally visible side effects (no pushes, no PR creation) | Verified: agents work in isolated worktrees, PR creation happens post-completion in approval gate | If an agent pushes from a speculative worktree, the discarded branch leaves orphan remote branches |
| A3 | Agent processes respect SIGTERM within 5s | Not verified for all adapters | SIGKILL fallback handles unresponsive agents; possible data corruption in worktree (acceptable since it's discarded) |
| A4 | Worktree cleanup is safe to run while agent process may still be writing | Partially verified: `--force` flag handles this | Possible git lock file contention; retry logic in WorktreeManager handles this |
| A5 | `TaskStatus.CANCELLED` exists and is in `TERMINAL_STATUSES` | Must verify in models.py | If missing, speculative tasks cannot be properly cancelled — RC-7 |
| A6 | Speculative waste cost is acceptable to users | User opt-in via config | Cost multiplier alert provides guardrails; disabled by default |
| A7 | Agent JWT tokens are short-lived and scoped to session | Verified: `jwt_tokens.py` creates session-scoped tokens | Low risk — token becomes useless after worktree deletion |

## Open Questions

- Should speculative agents be allowed to post to the bulletin board? (Risk: misleading cross-agent signals from work that may be discarded)
- Should the warm pool pre-create worktrees for *all* branches in a speculative group, or only for the most likely branch? (Requires condition probability estimation — likely overengineered for v1)
- Should partially completed speculative work be recoverable? (e.g., if fix-bugs completed 3 of 5 subtasks before being discarded, can those commits be cherry-picked?) Currently: no, discard is total.
- Should the cost multiplier alert integrate with the cost anomaly detector, or be a separate alerting path?
- What is the interaction with cross-model verification? Should speculative tasks skip verification since they may be discarded?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created from codebase discovery | — |
| 2026-04-11 | Verified `workflow_dsl.py` conditional edge system — DAGExecutor.resolve_edge() supports SATISFIED/SKIPPED/PENDING | Spec builds on this exact mechanism |
| 2026-04-11 | Verified `prepare_speculative_warm_pool` in task_lifecycle.py:211 — pre-warms for tasks 1 dep away | Spec extends this to full agent spawn, not just warm pool |
| 2026-04-11 | Verified `WorktreeManager.cleanup()` uses `--force` and deletes branch | Spec relies on this for discard cleanup |
| 2026-04-11 | RC-1: No /tasks/{id}/cancel endpoint — only /complete and /fail exist | Flagged as Critical; implementation must add this route |
| 2026-04-11 | RC-7: TaskStatus.CANCELLED exists in models.py but must confirm TERMINAL_STATUSES inclusion | Flagged for verification |
