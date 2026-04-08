# WORKFLOW: Disaster Recovery with Cross-Region Replication (ENT-010)
**Version**: 1.0
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-010 — Implement disaster recovery with cross-region replication

---

## Overview

Provides a complete disaster recovery solution for Bernstein's `.sdd/` state by extending the existing `backup_sdd`/`restore_sdd` local backup with: remote storage destinations (S3/GCS), cross-region WAL replication via the existing `WALReplicationManager`, automated failover detection based on replication health, and recovery runbook generation.  This workflow covers the full DR lifecycle from periodic backup through failure detection to recovery execution.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator (leader) | Schedules periodic backups, manages WAL replication, detects failures |
| `WALReplicationManager` | Replicates WAL entries to follower regions, tracks follower health |
| `backup_sdd()` / `restore_sdd()` | Creates/restores compressed state snapshots |
| Remote storage (S3/GCS) | Stores backup snapshots and replication state off-node |
| Follower node(s) | Receives replicated WAL entries, can be promoted to leader |
| Operator | Triggers manual failover, reviews runbooks, executes recovery |
| CLI (`bernstein dr`) | User-facing commands for backup, restore, status, runbook |

---

## Prerequisites

- `.sdd/` directory exists with persistent state subdirectories
- WAL is enabled (`compliance.wal_enabled = True`)
- For remote backup: cloud credentials available (AWS/GCP via secrets manager or env vars)
- For cross-region replication: `ReplicationConfig` with at least one `target_region`
- Network connectivity to remote storage and follower regions
- `cryptography` package available if encryption is required

---

## Sub-Workflows

This workflow contains four distinct sub-workflows that compose into the full DR solution:

1. **Periodic State Snapshot** — scheduled backup of `.sdd/` state
2. **Cross-Region WAL Replication** — continuous replication of WAL entries
3. **Failover Detection** — automated health monitoring and failover triggering
4. **Recovery Execution** — restore from backup or promote follower

---

## Sub-Workflow 1: Periodic State Snapshot

### Trigger
- Cron schedule (default: every 6 hours, configurable via `.sdd/config/dr.yaml`)
- Manual via `bernstein dr backup`
- Pre-shutdown hook (if graceful shutdown)

### STEP 1.1: Create Local Snapshot

**Actor**: Orchestrator / CLI
**Action**: Call `backup_sdd(sdd_path, dest, encrypt=True, password=key)` to create compressed, encrypted tarball of persistent `.sdd/` state. Excludes ephemeral dirs (runtime, logs, worktrees).
**Timeout**: 60s (depends on state size; 100MB state ~ 10s)
**Input**: `{ sdd_path: Path, dest: Path, encrypt: bool, password: str | None }`
**Output on SUCCESS**: `{ path: str, size_bytes: str, file_count: str, sha256: str }` -> GO TO STEP 1.2
**Output on FAILURE**:
  - `FAILURE(sdd_not_found)`: `.sdd/` directory missing -> Recovery: log critical, emit alert, skip this backup cycle
  - `FAILURE(disk_full)`: Cannot write tarball -> Recovery: log critical, try with reduced scope (metrics-only), emit alert
  - `FAILURE(encryption_error)`: Crypto failure -> Recovery: fall back to unencrypted backup + log security warning
  - `FAILURE(timeout)`: State too large, backup exceeded 60s -> Recovery: log, schedule with larger timeout next cycle

**Observable states during this step**:
  - Customer sees: nothing (background operation)
  - Operator sees: `[dr] Backup started, sdd_size=45MB` ... `[dr] Backup complete, sha256=abc123`
  - Database: New tarball at `dest`
  - Logs: `[disaster_recovery] Backup created: path=/backups/2026-04-08T12:00:00.tar.gz.enc size=12345678 files=342`

### STEP 1.2: Upload to Remote Storage

**Actor**: Orchestrator
**Action**: Upload the local snapshot to configured remote storage (S3 bucket or GCS bucket). Verify upload integrity via SHA-256 comparison. Apply retention policy (keep last N snapshots, delete older ones).
**Timeout**: 300s (depends on upload size and network)
**Input**: `{ local_path: Path, remote_dest: str, sha256: str, retention_count: int }`
**Output on SUCCESS**: `{ remote_path: str, upload_verified: bool }` -> GO TO STEP 1.3
**Output on FAILURE**:
  - `FAILURE(no_credentials)`: Cloud credentials not available -> Recovery: keep local backup only, log warning, emit bulletin `"DR backup not replicated — no cloud credentials"`
  - `FAILURE(upload_failed)`: Network error or permission denied -> Recovery: retry 3x with exponential backoff (5s, 15s, 45s). If all fail: keep local backup, log critical, emit alert
  - `FAILURE(integrity_mismatch)`: Remote SHA-256 differs from local -> Recovery: delete remote, re-upload once. If still mismatched: log critical, keep local only
  - `FAILURE(timeout)`: Upload took > 300s -> Recovery: abort upload, keep local backup, schedule retry next cycle

**Observable states during this step**:
  - Operator sees: `[dr] Uploading to s3://bernstein-dr/backups/...` then `[dr] Upload verified`
  - Logs: `[disaster_recovery] Remote upload: dest=s3://... size=12MB verified=True`

### STEP 1.3: Update Backup Manifest

**Actor**: Orchestrator
**Action**: Append entry to `.sdd/config/dr_manifest.json` with backup metadata (timestamp, path, remote path, SHA-256, file count, encryption flag). Prune manifest entries older than retention period.
**Timeout**: 1s
**Input**: `{ backup_metadata: dict, manifest_path: Path }`
**Output on SUCCESS**: `{ manifest_entries: int }` -> END (sub-workflow 1)
**Output on FAILURE**:
  - `FAILURE(io_error)`: Cannot write manifest -> Recovery: log warning (backup exists, just not tracked). Non-critical.

---

## Sub-Workflow 2: Cross-Region WAL Replication

### Trigger
- Every WAL append (continuous, event-driven)
- Periodic catch-up poll (every `retry_interval_s`, default 5s)

### STEP 2.1: Buffer WAL Entry

**Actor**: WAL writer (task server on state change)
**Action**: After writing a WAL entry locally, call `WALReplicationManager.append_entry()` to buffer it for replication.
**Timeout**: <1ms (in-memory append)
**Input**: `{ entry: ReplicableWALEntry }` where entry contains `{ seq: int, entry_hash: str, payload: dict, source_region: str, timestamp: float }`
**Output on SUCCESS**: Buffer updated -> GO TO STEP 2.2 (on next replication tick)
**Output on FAILURE**: Not possible (in-memory list append)

### STEP 2.2: Prepare Replication Batch

**Actor**: `WALReplicationManager`
**Action**: For each follower region, call `get_pending_entries(region)` to get entries not yet acknowledged. Batch up to `config.batch_size` entries (default 50).
**Timeout**: <1ms (in-memory filter)
**Input**: `{ region: str }`
**Output on SUCCESS**: `{ entries: list[ReplicableWALEntry], count: int }` -> GO TO STEP 2.3
**Output on FAILURE**: Not possible (in-memory operation)

### STEP 2.3: Send Batch to Follower

**Actor**: Replication transport (to be implemented — HTTP POST or message queue)
**Action**: Send the batch of WAL entries to the follower region's replication endpoint. Wait for acknowledgement.
**Timeout**: 30s per batch
**Input**: `{ region: str, entries: list[ReplicableWALEntry] }`
**Output on SUCCESS**: `{ region: str, entries_acked: int, seq: int }` -> call `acknowledge(region, seq)`, GO TO STEP 2.4
**Output on FAILURE**:
  - `FAILURE(timeout)`: Follower did not respond within 30s -> call `record_failure(region, "timeout")`, GO TO STEP 2.5
  - `FAILURE(network_error)`: Connection refused or DNS failure -> call `record_failure(region, error)`, GO TO STEP 2.5
  - `FAILURE(integrity_error)`: Follower reports hash mismatch -> log critical, mark entries for re-send with fresh hash, GO TO STEP 2.5

**Observable states during this step**:
  - Operator sees: `[replication] Sent 50 entries to eu-west, awaiting ack`
  - Logs: `[wal_replication] Batch to eu-west: entries=50, seq_range=1001-1050`

### STEP 2.4: Update Follower State on Success

**Actor**: `WALReplicationManager`
**Action**: Call `acknowledge(region, seq)` to update follower's `last_acked_seq`, reset `consecutive_failures`, set health to `HEALTHY`. Call `compact_buffer()` to remove entries acked by all followers.
**Timeout**: <1ms
**Input**: `{ region: str, seq: int }`
**Output**: Follower state updated -> END (until next entry or tick)

### STEP 2.5: Handle Replication Failure

**Actor**: `WALReplicationManager`
**Action**: `record_failure(region, error)` increments `consecutive_failures`. If `consecutive_failures >= max_retries`: set health to `UNREACHABLE`, emit alert. Schedule retry after `retry_interval_s`.
**Timeout**: N/A
**Input**: `{ region: str, error: str }`
**Output**:
  - If `consecutive_failures < max_retries`: Schedule retry -> GO TO STEP 2.3 after backoff
  - If `consecutive_failures >= max_retries`: Mark follower `UNREACHABLE`, emit bulletin `"Follower {region} unreachable after {N} attempts"` -> GO TO Sub-Workflow 3 (failover detection)

**Observable states**:
  - Operator sees: Warning logs escalating to critical
  - Logs: `[wal_replication] Replication failure for eu-west (attempt 3/3): timeout`

---

## Sub-Workflow 3: Failover Detection

### Trigger
- Follower health check poll (every 30s)
- Follower marked `UNREACHABLE` in Sub-Workflow 2
- Leader health probe failure (external monitoring)

### STEP 3.1: Health Assessment

**Actor**: Orchestrator
**Action**: Call `WALReplicationManager.check_health()` for all followers. Evaluate: lag entries vs `max_lag_entries`, follower health status, time since last ack.
**Timeout**: 5s
**Input**: `{ followers: dict[str, FollowerState], config: ReplicationConfig }`
**Output on SUCCESS**: `{ health: dict[str, FollowerHealth], degraded_regions: list[str] }` -> GO TO STEP 3.2 if any region unhealthy
**Output on FAILURE**: Not possible (in-memory check)

**Observable states**:
  - Operator sees: Periodic health summary in logs
  - Logs: `[dr] Health check: eu-west=HEALTHY(lag=3), ap-southeast=LAGGING(lag=150)`

### STEP 3.2: Evaluate Failover Conditions

**Actor**: Orchestrator
**Action**: Determine whether conditions warrant failover. Conditions:
  - **Leader failure**: External probe cannot reach leader (detected by follower or monitoring)
  - **Quorum loss**: More than half of followers are `UNREACHABLE` (for `AckPolicy.QUORUM`)
  - **Critical lag**: Follower lag exceeds `max_lag_entries * 10` (data loss risk)
**Timeout**: 1s
**Input**: `{ health_report: dict, ack_policy: AckPolicy, max_lag: int }`
**Output**:
  - `NO_ACTION`: All healthy or within acceptable parameters -> END (wait for next check)
  - `ALERT_ONLY`: Degraded but not critical -> emit bulletin, log warning -> END
  - `FAILOVER_RECOMMENDED`: Critical conditions met -> GO TO STEP 3.3

### STEP 3.3: Generate Recovery Runbook

**Actor**: Orchestrator
**Action**: Generate a timestamped, human-readable runbook document at `.sdd/docs/runbooks/DR-RUNBOOK-{timestamp}.md` containing:
  1. Current system state (leader region, follower states, last backup, replication lag)
  2. Recommended recovery action (promote follower, restore from backup, or both)
  3. Step-by-step CLI commands to execute recovery
  4. Data loss estimate (entries between last ack and failure)
  5. Post-recovery verification checklist
**Timeout**: 5s
**Input**: `{ system_state: dict, backup_manifest: dict, follower_states: dict }`
**Output on SUCCESS**: `{ runbook_path: str }` -> Emit bulletin `"DR runbook generated: {path}"`, notify operator
**Output on FAILURE**:
  - `FAILURE(io_error)`: Cannot write runbook -> Log critical, print runbook contents to stderr as fallback

**Observable states**:
  - Operator sees: Bulletin notification + runbook file on disk
  - Logs: `[dr] Recovery runbook generated: .sdd/docs/runbooks/DR-RUNBOOK-2026-04-08T14:30:00.md`

**Runbook format**:
```markdown
# Disaster Recovery Runbook
Generated: {timestamp}
Situation: {description}

## Current State
- Leader: {region} — {status}
- Followers: {region}: {health}, lag={N} entries
- Last backup: {timestamp}, {path}
- Estimated data loss: {N} WAL entries ({timespan})

## Recommended Action
{PROMOTE_FOLLOWER | RESTORE_FROM_BACKUP | PROMOTE_AND_RESTORE}

## Recovery Steps
1. `bernstein dr restore --from {backup_path} --decrypt`
2. `bernstein dr promote --region {region}`  # if follower promotion
3. `bernstein dr verify`  # integrity check

## Post-Recovery Verification
- [ ] Task server responds to GET /status
- [ ] WAL integrity verified (all hashes valid)
- [ ] Audit log chain unbroken
- [ ] Replication re-established to remaining followers
- [ ] Recent tasks verified against backup state
```

---

## Sub-Workflow 4: Recovery Execution

### Trigger
- Operator runs `bernstein dr restore`
- Automated recovery (if `auto_failover: true` in DR config — future)

### STEP 4.1: Pre-Recovery Validation

**Actor**: CLI / Operator
**Action**: Before restoring, validate:
  1. Backup file exists and SHA-256 matches manifest
  2. Target `.sdd/` is writable
  3. No Bernstein process is currently running (PID file check)
  4. If decryption needed, password/key is available
**Timeout**: 10s
**Input**: `{ source: Path, sdd_path: Path, decrypt: bool, password: str | None }`
**Output on SUCCESS**: Validation passed -> GO TO STEP 4.2
**Output on FAILURE**:
  - `FAILURE(backup_not_found)`: Source file missing -> abort, log error
  - `FAILURE(sha_mismatch)`: Backup corrupted -> abort, try previous backup from manifest
  - `FAILURE(process_running)`: Bernstein still running -> abort, instruct operator to stop first
  - `FAILURE(no_password)`: Encrypted backup but no password -> abort, instruct operator

### STEP 4.2: Restore State

**Actor**: `restore_sdd()`
**Action**: Extract backup tarball into `.sdd/`, replacing existing persistent state. Uses `filter="data"` to block path traversal attacks.
**Timeout**: 120s
**Input**: `{ source: Path, sdd_path: Path, decrypt: bool, password: str | None }`
**Output on SUCCESS**: `{ files_restored: str, source: str, sha256: str }` -> GO TO STEP 4.3
**Output on FAILURE**:
  - `FAILURE(decrypt_error)`: Wrong password or corrupted ciphertext -> abort, log error
  - `FAILURE(path_traversal)`: Malicious tarball detected -> abort, log critical security event
  - `FAILURE(disk_full)`: Cannot extract -> abort, log error, instruct operator to free space
  - `FAILURE(io_error)`: Permission denied -> abort, instruct operator

**Observable states**:
  - Operator sees: `[dr] Restoring from backup.tar.gz.enc...` then `[dr] Restore complete, 342 files`
  - Database: `.sdd/` state replaced with backup contents
  - Logs: `[disaster_recovery] Restore: files=342, source=backup.tar.gz.enc, sha256=...`

### STEP 4.3: Post-Recovery Integrity Verification

**Actor**: Orchestrator (on next startup after restore)
**Action**: Verify restored state integrity:
  1. Audit log HMAC chain verification (if `audit_hmac_chain` enabled)
  2. WAL integrity check (all entry hashes valid)
  3. Task backlog consistency (all YAML files parseable, no orphan references)
  4. Config files present and valid
**Timeout**: 30s
**Input**: `{ sdd_path: Path }`
**Output on SUCCESS**: `{ integrity: "verified", checks_passed: int }` -> GO TO STEP 4.4
**Output on FAILURE**:
  - `FAILURE(audit_chain_broken)`: HMAC chain has gap -> Log critical, mark audit log as "restored with gap", continue
  - `FAILURE(wal_corruption)`: WAL entry hash mismatch -> Truncate WAL to last valid entry, log warning
  - `FAILURE(backlog_corruption)`: Unparseable task YAML -> Move corrupt files to `.sdd/quarantine/`, log warning

### STEP 4.4: Re-establish Replication

**Actor**: Orchestrator
**Action**: If WAL replication was active before failure, re-initialize `WALReplicationManager` with remaining healthy followers. The promoted node becomes the new leader.
**Timeout**: 10s
**Input**: `{ config: ReplicationConfig, remaining_followers: list[str] }`
**Output on SUCCESS**: Replication re-established -> END
**Output on FAILURE**:
  - `FAILURE(no_followers)`: No healthy followers remaining -> Log warning, operate in standalone mode. DR is degraded until new followers configured.

---

## State Transitions

```
[operational] -> (backup scheduled)           -> [backing_up] -> [operational]
[operational] -> (follower UNREACHABLE)        -> [degraded]
[degraded]    -> (follower recovers)           -> [operational]
[degraded]    -> (quorum lost / leader down)   -> [failover_recommended]
[failover_recommended] -> (runbook generated)  -> [awaiting_recovery]
[awaiting_recovery] -> (operator restores)     -> [recovering]
[recovering] -> (restore + verify succeeds)    -> [operational]
[recovering] -> (restore fails)                -> [recovery_failed] -> retry or escalate
```

---

## Handoff Contracts

### Orchestrator -> backup_sdd (snapshot)

**Method**: `backup_sdd(sdd_path, dest, encrypt=True, password=key)`
**Payload**:
```python
{
    "sdd_path": "Path — .sdd/ directory",
    "dest": "Path — output tarball path",
    "encrypt": "bool — whether to encrypt",
    "password": "str | None — encryption password"
}
```
**Success response**:
```python
{
    "path": "str — final file path (may differ if encrypted)",
    "size_bytes": "str",
    "file_count": "str",
    "sha256": "str"
}
```
**Failure**: Raises `FileNotFoundError` (sdd missing) or `OSError` (disk full)
**Timeout**: 60s

### WALReplicationManager -> Follower (replication batch)

**Endpoint**: `POST /replication/batch` (to be implemented)
**Payload**:
```json
{
    "source_region": "us-east",
    "entries": [
        {
            "seq": 1001,
            "entry_hash": "sha256:...",
            "payload": {},
            "timestamp": 1712563200.0
        }
    ],
    "batch_id": "uuid"
}
```
**Success response**:
```json
{
    "acked_seq": 1050,
    "entries_applied": 50
}
```
**Failure response**:
```json
{
    "ok": false,
    "error": "hash mismatch at seq 1023",
    "code": "INTEGRITY_ERROR",
    "retryable": true
}
```
**Timeout**: 30s

### Orchestrator -> Remote Storage (upload)

**Endpoint**: S3 `PutObject` / GCS `objects.insert`
**Payload**: Binary tarball + metadata headers (SHA-256, timestamp, source region)
**Success response**: HTTP 200 + ETag
**Failure response**: HTTP 403 (creds), 507 (quota), 5xx (service)
**Timeout**: 300s — treated as FAILURE, retry with backoff

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Local backup tarball | Step 1.1 | Retention policy | File delete after remote upload confirmed |
| Remote backup object | Step 1.2 | Retention policy | S3/GCS delete of objects older than N |
| Backup manifest entry | Step 1.3 | Manifest pruning | Remove entries for deleted backups |
| WAL replication buffer | Step 2.1 | `compact_buffer()` | Remove entries acked by all followers |
| Recovery runbook | Step 3.3 | Manual cleanup | Operator deletes after recovery complete |
| Quarantined files | Step 4.3 | Manual cleanup | Operator reviews and deletes |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `backup_sdd()` only supports local file destinations — no S3/GCS upload | Critical | Step 1.2 | Must implement remote upload layer (new function or wrapper) |
| RC-2 | `WALReplicationManager` has no transport layer — `get_pending_entries()` returns entries but nothing sends them | Critical | Step 2.3 | Must implement replication transport (HTTP or message queue) |
| RC-3 | No periodic backup scheduling exists — backup is CLI-only | High | Trigger for Sub-Workflow 1 | Must add cron/scheduler integration or orchestrator-level timer |
| RC-4 | No failover detection logic exists — `check_health()` returns status but nothing evaluates failover conditions | High | Step 3.2 | Must implement failover evaluation in orchestrator |
| RC-5 | No runbook generation exists | High | Step 3.3 | Must implement `generate_runbook()` function |
| RC-6 | `restore_sdd()` does not validate manifest on restore (manifest is written in backup but not checked) | Medium | Step 4.1 | Must add manifest validation on restore |
| RC-7 | No SHA-256 verification of backup before restore (SHA is computed but not compared to manifest) | Medium | Step 4.1 | Must add integrity verification step |
| RC-8 | No PID file check before restore — operator could restore while Bernstein is running | Medium | Step 4.1 | Must add running-process guard |
| RC-9 | WAL replication buffer is unbounded if no followers ack — memory leak risk | Medium | Step 2.1 | Must add buffer size limit with oldest-entry eviction |
| RC-10 | No DR config file exists (`.sdd/config/dr.yaml`) — all DR settings are hardcoded or CLI flags | Low | All | Should add persistent DR configuration |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path backup + restore | Backup, then restore to clean dir | All persistent state restored, ephemeral excluded |
| TC-02: Encrypted backup round-trip | Backup with password, restore with same password | Decryption succeeds, state intact |
| TC-03: Wrong password on restore | Restore encrypted backup with wrong password | `decrypt_error`, abort, clear error message |
| TC-04: Path traversal blocked | Restore malicious tarball with `../` paths | `tarfile.OutsideDestinationError`, abort |
| TC-05: WAL replication happy path | Append entries, get pending, acknowledge | Follower state updated, buffer compacted |
| TC-06: Follower goes unreachable | 3 consecutive failures | Health set to `UNREACHABLE`, alert emitted |
| TC-07: Quorum check with ALL policy | 2 of 3 followers acked | `is_quorum_met` returns False |
| TC-08: Quorum check with QUORUM policy | 2 of 3 followers acked | `is_quorum_met` returns True |
| TC-09: Buffer compaction | All followers ack through seq 100 | Entries <= 100 removed from buffer |
| TC-10: Follower lag detection | Follower 200 entries behind, max_lag=100 | Health set to `LAGGING` |
| TC-11: Runbook generation | Failover conditions met | Runbook file created with correct state info |
| TC-12: Manifest integrity on restore | Backup with manifest, verify on restore | Manifest parsed, file count matches |
| TC-13: Restore while running blocked | PID file exists | Restore aborted with clear message |
| TC-14: Remote upload failure with retry | S3 returns 503 twice then 200 | Two retries, then success |
| TC-15: Remote upload total failure | S3 returns 503 three times | Local backup retained, alert emitted |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `.sdd/` state size stays under 1GB for reasonable backup times | Not verified — depends on project size | Large projects may need incremental backup strategy |
| A2 | WAL entries are serializable to JSON | Verified: `ReplicableWALEntry.payload` is `dict[str, Any]` | Low — if non-serializable values sneak in, replication breaks |
| A3 | Followers have compatible Bernstein versions | Not verified | Version mismatch could cause replication failures or silent data corruption |
| A4 | Cloud credentials (AWS/GCP) are available via standard SDK chain | Not verified — depends on deployment | If not available, remote backup is silently skipped |
| A5 | Single leader per cluster (no split-brain) | Assumed from current single-node architecture | If cluster mode allows multiple leaders, WAL replication can diverge |
| A6 | Backup encryption key is stored separately from backup | Not verified — operator responsibility | If key is lost, encrypted backups are unrecoverable |
| A7 | Network between leader and follower regions is available (not air-gapped) | Assumed | If air-gapped, replication transport must be adapted (e.g., file-based) |

## Open Questions

- What is the RPO (Recovery Point Objective) target? This determines backup frequency and acceptable replication lag.
- What is the RTO (Recovery Time Objective) target? This determines whether automated failover is required or manual runbook is sufficient.
- Should automated failover be supported (risky — split-brain potential) or should recovery always require operator action?
- For S3/GCS upload: should we use the cloud SDK directly, or go through the existing `SecretsProvider` / `SecretsConfig` infrastructure for credentials?
- Should there be a `bernstein dr status` command that shows replication health, last backup, and current RPO/RTO estimates?
- For WAL replication transport: HTTP POST to a known endpoint, or use a message queue (SQS/Pub-Sub) for better reliability?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created from code audit of disaster_recovery.py, wal_replication.py, disaster_recovery_cmd.py | 10 Reality Checker findings documented (RC-1 through RC-10) |
