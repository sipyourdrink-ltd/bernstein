# WORKFLOW: Write-Ahead Log Replication for High Availability

**Version**: 1.0
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-109 — Write-ahead log replication for high availability

---

## Overview

Replicates the orchestrator's write-ahead log (WAL) to a standby instance within the same region. If the primary (leader) fails, the standby replays uncommitted entries and promotes itself to leader. This is **single-region HA** — not cross-region DR (covered separately by ENT-010 `WORKFLOW-disaster-recovery-cross-region.md`). The goal is sub-minute recovery time for the orchestrator after a crash, without data loss for committed WAL entries.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Leader orchestrator | Primary instance that processes ticks, spawns agents, writes WAL entries |
| `WALWriter` | Appends hash-chained JSONL entries with `os.fsync()` on every write |
| `WALReplicationManager` | Buffers WAL entries, serves them to followers, tracks follower health |
| Standby orchestrator | Follower instance that pulls WAL entries, replays to stay in sync, promotes on leader failure |
| `WALReader` | Reads and verifies hash chain integrity of WAL files |
| `WALReplayEngine` | Replays WAL entries on standby to rebuild state (creates tasks, claims, etc.) |
| `IdempotencyStore` | Prevents double-execution of replayed entries via `{decision_type}:{entry_hash}` keys |
| `SupervisorProcess` | Monitors leader health, triggers failover after sustained unresponsiveness |
| `TaskStore` | In-memory task state rebuilt from WAL replay on standby promotion |
| `CheckpointManager` | Periodic snapshots that bound WAL replay length |
| Shared filesystem / Network | Transport layer for WAL entry delivery between leader and standby |

---

## Prerequisites

- WAL is enabled (`compliance.wal_enabled = True` or always-on)
- Leader writes WAL to `.sdd/runtime/wal/{run_id}.wal.jsonl` (verified: `wal.py`)
- `WALReplicationManager` exists (verified: `wal_replication.py`) but is **not wired into orchestrator startup**
- `server_supervisor.py` exists with health check loop (verified) but monitors only leader — no standby awareness
- Standby instance has access to leader's replication endpoint (HTTP or shared filesystem)
- Standby instance has its own `.sdd/` directory with separate runtime state
- Both instances run the same Bernstein version (WAL format compatibility)

---

## Sub-Workflows

1. **Steady-State Replication** — leader continuously streams WAL entries to standby
2. **Standby Replay** — standby applies received WAL entries to maintain hot state
3. **Failure Detection** — supervisor detects leader failure
4. **Failover Promotion** — standby promotes to leader and takes over
5. **Leader Rejoin** — old leader restarts and rejoins as standby

---

## Sub-Workflow 1: Steady-State Replication

### Trigger
Leader orchestrator starts and `replication.enabled = True` in config.

### STEP 1.1: Initialize Replication Manager
**Actor**: Leader orchestrator (startup)
**Action**: Create `WALReplicationManager` with configured `AckPolicy` (default `LEADER_ONLY` for HA; `QUORUM` is optional for stronger durability). Register the standby as a follower with initial state `UNKNOWN`.
**Input**: `{ ack_policy: AckPolicy, followers: [{ region: string, endpoint: string }], max_lag_entries: int = 100, max_retries: int = 3 }`
**Output on SUCCESS**: Manager initialized, follower registered with state `UNKNOWN` → GO TO STEP 1.2
**Output on FAILURE**:
  - `FAILURE(invalid_config)`: Missing or invalid replication config → log error, start without replication (degraded mode)

**Observable states**:
  - Operator sees: "Replication enabled. Standby: {endpoint}. Ack policy: {policy}."
  - Logs: `[replication] initialized ack_policy={policy} followers={count}`

### STEP 1.2: Hook WALWriter to Replication Buffer
**Actor**: Leader orchestrator
**Action**: After every `WALWriter.append()` call, also call `WALReplicationManager.append_entry()` to buffer the entry for follower consumption. This is the **critical integration point** that does not exist today.
**Input**: `{ wal_entry: WALEntry }`
**Output on SUCCESS**: Entry buffered → follower will pull on next poll
**Output on FAILURE**:
  - `FAILURE(buffer_overflow)`: Buffer exceeds memory limit → compact oldest entries, log warning

**Observable states**:
  - Logs: `[replication] entry buffered seq={seq} type={decision_type}`

**Timing**: This hook MUST execute after `os.fsync()` in `WALWriter.append()` completes — the entry must be durable on the leader before being offered to followers.

### STEP 1.3: Serve Pull Endpoint
**Actor**: Leader task server (new route)
**Action**: Expose `GET /replication/wal?since_seq={seq}&limit={n}` endpoint. Returns batch of WAL entries with seq > since_seq. Authenticated via bearer token (same auth as task server).
**Input**: `{ since_seq: int, limit: int = 100 }`
**Output on SUCCESS**: `{ entries: WALEntry[], leader_seq: int, leader_run_id: string }`
**Output on FAILURE**:
  - `FAILURE(seq_too_old)`: Requested seq is older than buffer start → return `{ error: "seq_too_old", oldest_available: int }`. Follower must do full state sync.
  - `FAILURE(auth_error)`: Invalid token → 401

**Observable states**:
  - Logs: `[replication] pull request from={follower_id} since_seq={seq} returned={count}`

### STEP 1.4: Track Follower Health
**Actor**: `WALReplicationManager`
**Action**: On each successful pull, update `FollowerState.last_acked_seq` and reset failure counter. On each failed pull (timeout or error), increment `consecutive_failures`. Transition health states:
  - `UNKNOWN` → first successful pull → `HEALTHY`
  - `HEALTHY` → lag > `max_lag_entries` → `LAGGING`
  - `LAGGING` → lag < `max_lag_entries` → `HEALTHY`
  - `HEALTHY`/`LAGGING` → `consecutive_failures >= max_retries` → `UNREACHABLE`
  - `UNREACHABLE` → successful pull → `HEALTHY`
**Input**: `{ follower_id: string, pull_result: success|failure, acked_seq: int }`

**Observable states**:
  - Operator sees: Follower health in `bernstein status` — HEALTHY (green), LAGGING (yellow), UNREACHABLE (red)
  - Logs: `[replication] follower={id} health={state} lag={entries_behind}`

### STEP 1.5: Compact Replication Buffer
**Actor**: `WALReplicationManager`
**Action**: Periodically (every 60s or after N entries), remove entries from the in-memory buffer that all followers have acked. This bounds memory usage.
**Input**: `{ min_acked_seq: int }` (minimum across all followers)
**Output**: Buffer trimmed to entries with seq > min_acked_seq.

**Observable states**:
  - Logs: `[replication] buffer compacted min_acked={seq} buffer_size={remaining}`

---

## Sub-Workflow 2: Standby Replay

### Trigger
Standby orchestrator starts in `standby` mode.

### STEP 2.1: Initialize Standby State
**Actor**: Standby orchestrator
**Action**: Load last checkpoint from `.sdd/runtime/checkpoints/`. If no checkpoint, start from seq=0 (full replay). Initialize `IdempotencyStore` from `.sdd/runtime/wal/idempotency.jsonl`.
**Input**: `{ checkpoint_path?: Path, idempotency_path: Path }`
**Output on SUCCESS**: `{ start_seq: int, task_store: TaskStore }` → GO TO STEP 2.2
**Output on FAILURE**:
  - `FAILURE(corrupt_checkpoint)`: Checkpoint fails validation → start from seq=0 (full replay), warn operator

**Observable states**:
  - Operator sees: "Standby starting. Replaying from seq {start_seq}."
  - Logs: `[standby] init start_seq={seq} checkpoint={found|missing}`

### STEP 2.2: Pull WAL Entries from Leader
**Actor**: Standby orchestrator (poll loop)
**Action**: Every `poll_interval_s` (default 2s), call `GET /replication/wal?since_seq={last_applied_seq}` on the leader.
**Timeout**: 10s per poll
**Input**: `{ leader_endpoint: string, last_applied_seq: int }`
**Output on SUCCESS**: `{ entries: WALEntry[] }` → GO TO STEP 2.3 for each entry
**Output on FAILURE**:
  - `FAILURE(network_timeout)`: Leader unreachable → increment failure counter, retry in `poll_interval_s` → if failures >= `failover_threshold`, GO TO Sub-Workflow 3
  - `FAILURE(seq_too_old)`: Leader's buffer doesn't go back far enough → trigger full state sync (STEP 2.5)
  - `FAILURE(auth_error)`: → log error, halt standby, operator must fix config

**Observable states**:
  - Logs: `[standby] poll leader={endpoint} since_seq={seq} received={count}`

### STEP 2.3: Verify Entry Chain Integrity
**Actor**: `WALReader`
**Action**: For each received entry, verify `prev_hash` matches the hash of the last applied entry. If chain is broken, the entries are corrupt or out of order.
**Input**: `{ entry: WALEntry, expected_prev_hash: string }`
**Output on SUCCESS**: Chain valid → GO TO STEP 2.4
**Output on FAILURE**:
  - `FAILURE(chain_broken)`: `prev_hash` mismatch → ALERT operator, trigger full state sync (STEP 2.5)

**Observable states**:
  - Logs: `[standby] chain_verify seq={seq} result={valid|broken}`

### STEP 2.4: Apply WAL Entry
**Actor**: `WALReplayEngine`
**Action**: Check `IdempotencyStore` for `{decision_type}:{entry_hash}`. If already applied, skip. Otherwise, replay the entry:
  - `task_created` → create task in standby TaskStore
  - `task_claimed` → update task status to CLAIMED
  - `task_completed` → update task status to DONE
  - `task_failed` → update task status to FAILED
  - `agent_spawned` → record agent session (but do NOT spawn an actual agent)
  - `agent_killed` → record agent termination
  - `tick_start` → skip (informational)
  - `wal_recovery_ack` → skip (leader-only)
After successful apply, write entry hash to `IdempotencyStore`.
**Input**: `{ entry: WALEntry, task_store: TaskStore, idempotency_store: IdempotencyStore }`
**Output on SUCCESS**: State updated → continue polling
**Output on FAILURE**:
  - `FAILURE(replay_error)`: Entry cannot be applied (e.g., task not found for `task_completed`) → log warning, record in error log, continue with next entry

**Observable states**:
  - Operator sees: "Standby lag: {leader_seq - last_applied_seq} entries"
  - Logs: `[standby] applied seq={seq} type={decision_type}`
  - Database (standby): TaskStore reflects leader state with ≤ poll_interval_s delay

### STEP 2.5: Full State Sync (Fallback)
**Actor**: Standby orchestrator
**Action**: When the standby cannot catch up via WAL replay (buffer too old, chain broken), request a full state snapshot from the leader via `GET /replication/snapshot`. The leader responds with the current `tasks.jsonl` + latest checkpoint + current WAL position. Standby rebuilds from this snapshot.
**Timeout**: 60s
**Input**: `{ leader_endpoint: string }`
**Output on SUCCESS**: `{ tasks_jsonl: bytes, checkpoint: bytes, leader_seq: int }` → rebuild TaskStore, resume polling from `leader_seq`
**Output on FAILURE**:
  - `FAILURE(snapshot_too_large)`: Snapshot exceeds transfer limit → operator must intervene
  - `FAILURE(leader_unreachable)`: → increment failure counter → if >= threshold, GO TO Sub-Workflow 3

**Observable states**:
  - Operator sees: "FULL STATE SYNC in progress..."
  - Logs: `[standby] full_sync started reason={chain_broken|seq_too_old}`

---

## Sub-Workflow 3: Failure Detection

### Trigger
Standby's poll loop fails to reach leader for `failover_threshold` consecutive attempts.

### STEP 3.1: Confirm Leader Unreachable
**Actor**: Standby orchestrator
**Action**: Before promoting, make 3 additional confirmation attempts to the leader with 5s spacing. This prevents false positives from transient network blips.
**Timeout**: 5s per attempt, 15s total
**Input**: `{ leader_endpoint: string, confirmation_attempts: 3 }`
**Output on SUCCESS (leader responds)**: False alarm → reset failure counter, resume STEP 2.2
**Output on FAILURE (all 3 fail)**: Leader confirmed unreachable → GO TO STEP 3.2

**Observable states**:
  - Operator sees: "WARNING: Leader unreachable. Confirming... ({n}/3)"
  - Logs: `[failover] confirming leader_unreachable attempt={n}`

### STEP 3.2: Check Fencing Token
**Actor**: Standby orchestrator
**Action**: Acquire a distributed lock (if using Redis backend via `RedisCoordinator`) or write a fencing file (if file-based). This prevents split-brain: two instances both believing they are leader.
**Input**: `{ lock_key: "bernstein:leader", lock_ttl_ms: 30000 }`
**Output on SUCCESS (lock acquired)**: → GO TO STEP 3.3
**Output on FAILURE**:
  - `FAILURE(lock_held)`: Another node holds the leader lock → another standby promoted first, or leader recovered. Remain standby.
  - `FAILURE(lock_unavailable)`: Redis/lock service unreachable → ALERT operator, do NOT promote (split-brain risk)

**Observable states**:
  - Logs: `[failover] fencing_token result={acquired|held|unavailable}`

### STEP 3.3: Evaluate Promotion Safety
**Actor**: Standby orchestrator
**Action**: Check replication lag. If `leader_seq - last_applied_seq > max_safe_lag` (default 50), warn operator that promotion will lose entries. If lag == 0, safe to promote with no data loss.
**Input**: `{ leader_seq: int, last_applied_seq: int, max_safe_lag: int }`
**Output on SUCCESS (lag within threshold)**: → GO TO Sub-Workflow 4
**Output on FAILURE**:
  - `FAILURE(excessive_lag)`: Lag exceeds threshold → ALERT operator "Promotion would lose {n} WAL entries. Proceed? [y/N]" (or auto-promote if `auto_failover: true` in config)

**Observable states**:
  - Operator sees: "Promotion safety check: lag={n} entries, max_safe_lag={threshold}"
  - Logs: `[failover] safety_check lag={n} safe={bool}`

---

## Sub-Workflow 4: Failover Promotion

### Trigger
Sub-Workflow 3 completes with lock acquired and safety check passed.

### STEP 4.1: Replay Remaining WAL Entries
**Actor**: Standby `WALReplayEngine`
**Action**: Apply any buffered-but-unapplied WAL entries to bring standby state to the most recent point possible. This is the final catch-up before promotion.
**Input**: `{ buffered_entries: WALEntry[] }`
**Output on SUCCESS**: All entries applied → GO TO STEP 4.2

**Observable states**:
  - Logs: `[failover] final_replay entries={count}`

### STEP 4.2: Reconcile Stale Tasks
**Actor**: Standby `TaskStore`
**Action**: Any tasks in `CLAIMED` or `IN_PROGRESS` status are reset to `OPEN`. The agents that claimed them were running on the old leader and are presumed dead. This mirrors `recover_stale_claimed_tasks()` from `task_store.py`.
**Input**: `{ task_store: TaskStore }`
**Output on SUCCESS**: `{ reconciled_count: int }` → GO TO STEP 4.3

**Observable states**:
  - Logs: `[failover] reconciled stale_tasks={count}`

### STEP 4.3: Start New WAL
**Actor**: Promoted orchestrator
**Action**: Create a new WAL file for this run: `.sdd/runtime/wal/{new_run_id}.wal.jsonl`. Write `wal_recovery_ack` entry referencing the old leader's last known seq. Initialize `WALWriter` with fresh hash chain from `GENESIS_HASH`.
**Input**: `{ run_id: string, old_leader_last_seq: int }`
**Output on SUCCESS**: New WAL initialized → GO TO STEP 4.4

**Observable states**:
  - Logs: `[failover] new_wal created run_id={id} recovery_from_seq={seq}`

### STEP 4.4: Start Task Server
**Actor**: Promoted orchestrator
**Action**: Bind to the task server port (8052). If the old leader's port is still occupied (zombie process), wait up to 30s for it to release.
**Timeout**: 30s for port acquisition
**Input**: `{ host: string, port: 8052 }`
**Output on SUCCESS**: Server listening → GO TO STEP 4.5
**Output on FAILURE**:
  - `FAILURE(port_in_use)`: After 30s, port still held → ALERT operator, attempt alternate port or kill zombie

**Observable states**:
  - Logs: `[failover] task_server binding port={port}`

### STEP 4.5: Start Orchestrator Tick Loop
**Actor**: Promoted orchestrator
**Action**: Begin the main tick loop as leader. Resume spawning agents, processing task completions, writing WAL entries.
**Input**: `{ task_store: TaskStore, wal_writer: WALWriter }`
**Output on SUCCESS**: Orchestrator running as leader → GO TO STEP 4.6

**Observable states**:
  - Operator sees: "PROMOTED TO LEADER. Recovery time: {seconds}s. Tasks reconciled: {n}."
  - Logs: `[failover] promotion_complete recovery_time_s={elapsed}`

### STEP 4.6: Initialize Replication for New Standby
**Actor**: New leader
**Action**: If a new standby is configured (or the old leader will rejoin), initialize `WALReplicationManager` to start serving WAL entries to followers. Until a standby connects, the leader runs without HA protection.

**Observable states**:
  - Operator sees: "WARNING: No standby connected. Running without HA protection."
  - Logs: `[replication] waiting for standby connection`

---

## Sub-Workflow 5: Leader Rejoin (as Standby)

### Trigger
Old leader process restarts after crash recovery.

### STEP 5.1: Detect Fencing Token Lost
**Actor**: Restarted process
**Action**: Attempt to acquire leader lock. If lock is held by the promoted standby, this process must join as standby.
**Input**: `{ lock_key: "bernstein:leader" }`
**Output on SUCCESS (lock acquired)**: No failover occurred — resume as leader (rare: standby crashed before promoting)
**Output on FAILURE (lock held)**: → GO TO STEP 5.2

### STEP 5.2: Join as Standby
**Actor**: Old leader (now standby)
**Action**: Start in standby mode. Connect to the new leader's replication endpoint. Begin pulling WAL entries and replaying.
**Input**: `{ new_leader_endpoint: string }`
**Output on SUCCESS**: → resume Sub-Workflow 2 (Standby Replay)

**Observable states**:
  - Operator sees: "Old leader rejoined as standby. Syncing from new leader."
  - Logs: `[rejoin] started as_standby new_leader={endpoint}`

---

## State Transitions

```
Leader States:
[starting] -> (init complete) -> [leading]
[leading] -> (crash / unresponsive) -> [dead]
[dead] -> (process restarts, lock held by other) -> [standby]
[dead] -> (process restarts, lock not held) -> [leading] (no failover occurred)

Standby States:
[starting] -> (init complete) -> [syncing]
[syncing] -> (caught up to leader) -> [hot_standby]
[hot_standby] -> (leader unreachable confirmed) -> [promoting]
[promoting] -> (lock acquired, replay done) -> [leading]
[promoting] -> (lock held by other) -> [hot_standby] (another standby won)

Follower Health (per WALReplicationManager):
[UNKNOWN] -> (first successful pull) -> [HEALTHY]
[HEALTHY] -> (lag > max_lag_entries) -> [LAGGING]
[LAGGING] -> (lag < max_lag_entries) -> [HEALTHY]
[HEALTHY|LAGGING] -> (consecutive_failures >= max_retries) -> [UNREACHABLE]
[UNREACHABLE] -> (successful pull) -> [HEALTHY]
```

---

## Handoff Contracts

### Leader `WALWriter.append()` → `WALReplicationManager.append_entry()`
**Payload**: `ReplicableWALEntry { seq: int, prev_hash: str, entry_hash: str, timestamp: float, decision_type: str, inputs: dict, output: dict, actor: str, committed: bool }`
**Success**: Entry buffered for follower consumption
**Failure**: Buffer overflow → log warning, compact oldest entries
**Timeout**: N/A (in-process, synchronous)

### Standby → Leader `GET /replication/wal`
**Endpoint**: `GET /replication/wal?since_seq={seq}&limit={n}`
**Payload**: query params
**Success response**:
```json
{
  "entries": [{"seq": 1, "prev_hash": "...", "entry_hash": "...", "timestamp": 1234.5, "decision_type": "task_created", "inputs": {}, "output": {}, "actor": "orchestrator", "committed": true}],
  "leader_seq": 42,
  "leader_run_id": "abc123"
}
```
**Failure response**:
```json
{
  "ok": false,
  "error": "seq_too_old",
  "code": "REPLICATION_SEQ_TOO_OLD",
  "oldest_available": 10,
  "retryable": false
}
```
**Timeout**: 10s

### Standby → Leader `GET /replication/snapshot`
**Endpoint**: `GET /replication/snapshot`
**Success response**: Streamed tarball of `tasks.jsonl` + latest checkpoint + WAL position metadata
**Failure response**: `{ "ok": false, "error": "snapshot_in_progress", "retryable": true }`
**Timeout**: 60s

### `WALReplayEngine` → `TaskStore`
**Payload**: Individual state mutations: `create_task(task)`, `update_status(task_id, status)`, `record_agent(session)`, etc.
**Success**: State updated
**Failure**: `TaskStoreUnavailable` → retry with backoff (3 attempts). If exhausted, log error, skip entry, continue.
**Timeout**: N/A (in-process)

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Leader lock / fencing token | STEP 3.2 | Leader shutdown (graceful) or TTL expiry | Lock release or TTL |
| New WAL file | STEP 4.3 | Normal WAL rotation / archival | File delete after archive |
| Standby idempotency store | STEP 2.1 | Standby restart (rebuilt from disk) | File overwrite |
| Standby in-memory TaskStore | STEP 2.4 | Standby restart | Process exit |
| Old leader's PID file | Pre-existing | Promoted standby or supervisor | File delete |
| Replication buffer (in-memory) | STEP 1.2 | STEP 1.5 compaction or process exit | GC after compaction |

---

## Critical Timing Constraints

| Constraint | Budget | Mechanism | Risk if violated |
|---|---|---|---|
| Leader health check interval | 10s | `server_supervisor.py` polls `/health` | Slow failure detection |
| Standby poll interval | 2s | Standby poll loop | Higher replication lag |
| Failover confirmation | 15s (3 × 5s) | STEP 3.1 | False positive promotion (split-brain) |
| Promotion total time | < 60s | STEP 4.1 through 4.5 | Agents idle waiting for task server |
| Fencing token TTL | 30s | Redis lock or file lock | Lock expires during promotion → split-brain |
| Max safe replication lag | 50 entries | STEP 3.3 | Data loss on failover |
| WAL entry fsync | Per-entry | `WALWriter.append()` via `os.fsync()` | Entry durability gap |

---

## Split-Brain Prevention

Split-brain is the highest-severity failure mode. Two instances acting as leader simultaneously will:
- Create conflicting WAL chains (irreversible divergence)
- Spawn duplicate agents for the same tasks
- Produce conflicting task state transitions

**Prevention mechanisms:**
1. **Fencing token** (STEP 3.2): Only one instance holds the leader lock at a time. Lock TTL must exceed promotion time.
2. **Confirmation attempts** (STEP 3.1): 3 additional checks prevent promotion on transient network issues.
3. **Old leader detection** (STEP 5.1): Restarted leader checks lock before resuming — if lock is held, it joins as standby.
4. **Task server port binding** (STEP 4.4): Only one process can bind port 8052 on the same host.

**Remaining risk**: If the lock service (Redis) is partitioned from both instances, neither can verify the other's status. In this case, the system should **refuse to promote** (STEP 3.2 `FAILURE(lock_unavailable)`), not promote optimistically.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `WALReplicationManager` is not instantiated by the orchestrator | Critical | Sub-Workflow 1, STEP 1.1 | Must add initialization in `orchestrator.py` startup |
| RC-2 | No `/replication/wal` or `/replication/snapshot` HTTP routes exist | Critical | Sub-Workflow 1, STEP 1.3 | Must add routes to task server |
| RC-3 | `WALWriter.append()` does not call `WALReplicationManager.append_entry()` | Critical | Sub-Workflow 1, STEP 1.2 | Must hook WAL writes into replication buffer |
| RC-4 | `WALReplicationManager` has no HTTP transport — only in-memory buffer | Critical | Sub-Workflow 1 | Must implement pull-based HTTP transport |
| RC-5 | `IdempotencyStore` is per-node — standby needs its own | High | Sub-Workflow 2, STEP 2.4 | Standby must initialize independent IdempotencyStore |
| RC-6 | `SupervisorProcess` only monitors leader health, has no standby awareness | High | Sub-Workflow 3 | Must extend supervisor or add separate failover monitor |
| RC-7 | No standby mode exists in the orchestrator | Critical | Sub-Workflow 2 | Must implement standby startup mode that skips tick loop but runs replay loop |
| RC-8 | `RedisCoordinator` exists for distributed locking but is only used by store_redis | Medium | Sub-Workflow 3, STEP 3.2 | Can reuse for fencing token, but needs extraction to shared util |
| RC-9 | No `bernstein status` integration for replication health | Medium | Sub-Workflow 1, STEP 1.4 | Must surface follower health in status dashboard |
| RC-10 | `_MAX_REPLAY_AGE_S = 3600` in `wal_replay.py` may be too long for HA | Medium | Sub-Workflow 2, STEP 2.4 | HA replay should have tighter age limit (e.g., 300s) |
| RC-11 | No `committed=False` → `committed=True` WAL entry pair for replication-critical writes | High | Sub-Workflow 2, STEP 2.4 | Standby must handle uncommitted entries correctly during replay |
| RC-12 | Checkpoint does not include `wal_position` in practice (only in schema) | Medium | Sub-Workflow 2, STEP 2.1 | Must verify checkpoint actually stores WAL seq for resume point |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Steady-state replication | Leader writes 10 WAL entries | Standby receives all 10 within 2 × poll_interval |
| TC-02: Leader crash, clean failover | Kill leader process | Standby promotes within 60s, all committed entries preserved |
| TC-03: Leader crash, standby lagging | Kill leader with standby 20 entries behind | Standby replays remaining entries, then promotes |
| TC-04: Leader crash, excessive lag | Kill leader with standby 100+ entries behind | Standby warns operator about potential data loss before promoting |
| TC-05: Transient network failure | Block standby→leader for 10s | Standby retries, does NOT promote (below threshold) |
| TC-06: Split-brain prevention | Both instances try to acquire leader lock | Only one succeeds; other remains standby |
| TC-07: Chain integrity violation | Inject corrupted entry in replication stream | Standby detects chain break, triggers full state sync |
| TC-08: Idempotent replay | Replay same entry twice | Second replay is skipped via IdempotencyStore |
| TC-09: Old leader rejoin | Restart old leader after failover | Old leader detects lock held, joins as standby |
| TC-10: Full state sync | Standby requests seq older than buffer | Leader serves snapshot, standby rebuilds |
| TC-11: Lock service unavailable | Redis unreachable during failover | Standby refuses to promote, alerts operator |
| TC-12: Port conflict on promotion | Old leader zombie holds port 8052 | Promoted standby waits up to 30s, then alerts |
| TC-13: Uncommitted WAL entries | Leader crashes between committed=False and committed=True | Standby skips uncommitted entries, reconciles affected tasks |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Leader and standby run on the same host or share a network | Not verified — deployment topology is undefined | If on different hosts, need auth + encryption for replication traffic |
| A2 | Redis is available for distributed locking in HA mode | Partially verified: `RedisCoordinator` exists in `store_redis.py` | If Redis is unavailable, fencing token cannot be acquired → split-brain risk |
| A3 | Only one standby is needed for single-region HA | Design decision | Multiple standbys add complexity (leader election) but improve availability |
| A4 | WAL format is stable across minor versions | Not verified | Version mismatch between leader and standby could break replay |
| A5 | `os.fsync()` in `WALWriter.append()` is sufficient for durability | Verified: `wal.py` calls `os.fsync(fd)` on every entry | Low risk on local disk; NFS/network filesystem may not honor fsync |
| A6 | Task server port 8052 is the canonical port | Verified: hardcoded in multiple places | If configurable, standby must know the port dynamically |
| A7 | Standby does NOT spawn agents during replay | Design decision | Spawning during replay would create duplicate agents |
| A8 | Checkpoint `wal_position` field is populated correctly | Needs verification: `checkpoint.py` schema has field, but orchestrator may not set it | Standby would replay from seq=0 on every restart |

## Open Questions

- Should HA be active-passive (one standby) or active-active (multiple standbys with leader election)?
- What is the transport layer? HTTP pull (simplest, spec'd here) or persistent TCP stream (lower latency)?
- Should the standby maintain a full TaskStore in memory, or only the WAL — rebuilding TaskStore on promotion?
- How is the standby discovered? Static config, DNS, service discovery (Consul/etcd)?
- Should the system support automatic failback (old leader automatically reclaims leadership)?
- What is the RPO (Recovery Point Objective) — zero data loss (`QUORUM` ack) or best-effort (`LEADER_ONLY`)?
- Should replication traffic be encrypted (TLS) and/or authenticated beyond the bearer token?
- How does this interact with the multi-tenant isolation (ENT-001) — is WAL scoped per tenant?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created. 12 Reality Checker findings documented. | — |
