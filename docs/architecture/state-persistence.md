# State Persistence

**Where does my task graph go between orchestrator runs?**

Everything Bernstein needs to resume after a crash lives in the project's
`.sdd/` directory. Task specs are YAML files, every state transition is
appended to a hash-chained write-ahead log before being applied, and large
artifacts are stored content-addressed. On restart the orchestrator reads
the WAL, replays uncommitted entries through an idempotency filter, and
resumes the tick loop where it left off.

If you only have time for one sentence: **kill `bernstein run`, restart it
in the same workdir, and your task graph comes back**. The rest of this
document explains why.

---

## The `.sdd/` directory layout

Bernstein writes everything under `<workdir>/.sdd/`. The split between
durable (commit-worthy and crash-recoverable) and ephemeral (regenerated
each run) is intentional:

| Path | Purpose | Durability |
|------|---------|------------|
| `.sdd/backlog/open/*.yaml` | Tasks waiting to be picked up by the orchestrator | durable - author by hand or by tool |
| `.sdd/backlog/claimed/*.yaml` | Tasks the orchestrator has ingested into the in-memory store | durable - moved here by `ingest_backlog()` |
| `.sdd/backlog/closed/*.yaml` | Tasks the janitor has finalised | durable - append-only history |
| `.sdd/backlog/issues/*.yaml` | Tasks synced from GitHub issues | durable - re-synced each run |
| `.sdd/runtime/wal/<run-id>.wal.jsonl` | Hash-chained write-ahead log of every orchestrator decision | durable for crash recovery |
| `.sdd/runtime/wal/uncommitted.idx.json` | Sidecar index of in-flight entries | rebuildable from WAL |
| `.sdd/runtime/wal/idempotency.jsonl` | Replay deduplication markers | durable - survives crashes |
| `.sdd/metrics/*.jsonl` | Cost ledger, cascade chain reports, file-health scores | durable - append-only |
| `.sdd/cas/{xx}/{sha256}` | Content-addressed artifact blobs | durable - deduplicated |
| `.sdd/audit/merkle/seal-*.json` | Tamper-evident audit log seals | durable - compliance evidence |
| `.sdd/runtime/` (other) | PIDs, sockets, log fragments, agent signals | ephemeral - never commit |
| `.sdd/worktrees/` | Per-agent git worktrees | ephemeral - recreated on demand |
| `.sdd/config/mcp_servers.yaml` | MCP server auto-discovery catalog | durable - config |

Rule of thumb: anything under `.sdd/runtime/` other than `wal/` is
disposable. Anything under `.sdd/backlog/`, `.sdd/runtime/wal/`,
`.sdd/metrics/`, `.sdd/cas/` or `.sdd/audit/` is needed to reconstruct
state.

The repo's `.gitignore` excludes the volatile pieces; the durable pieces
can be checked in or shipped to remote storage.

---

## WAL invariants

The write-ahead log is a hash-chained append-only JSONL file. There is one
file per orchestrator run:

```text
.sdd/runtime/wal/<run-id>.wal.jsonl
```

Every line is a JSON object representing one orchestrator decision.
`WALEntry` fields:

```python
@dataclass(frozen=True)
class WALEntry:
    seq: int                # 0-based monotonic sequence
    prev_hash: str          # SHA-256 of previous entry; first is GENESIS_HASH
    entry_hash: str         # SHA-256 over (seq, prev_hash, timestamp, ...)
    timestamp: float        # Unix seconds
    decision_type: str      # e.g. "task_spawn_intent", "task_spawn_confirmed"
    inputs: dict[str, Any]  # what we decided on
    output: dict[str, Any]  # the resulting action's primary key(s)
    actor: str              # which orchestrator component wrote it
    committed: bool = True  # False = pre-execution intent
```

Source: `src/bernstein/core/persistence/wal.py`.

Three invariants make the WAL load-bearing for recovery:

1. **Append-only.** `WALWriter.append()` only ever calls `f.write(...)
   + f.flush() + os.fsync(...)` (`wal.py`). No mutation, no
   truncation. A torn write that leaves a partial trailing line is
   tolerated by the reader (`wal.py`) and by the tail-reader
   (`wal.py`).
2. **fsync per entry.** Every successful `append()` returns only after
   the line is on stable storage (`wal.py`). A process crash
   immediately after `append()` returns cannot lose the entry.
3. **Hash chain integrity.** `prev_hash` of entry N+1 equals
   `entry_hash` of entry N. `WALReader.verify_chain()` walks the file
   and reports any break (`wal.py`). A tamper, a torn write,
   or an out-of-order append is detectable.

The pattern for a state transition that has external side effects (e.g.
spawning an agent process) is:

```text
WALWriter.append(decision_type="task_spawn_intent", committed=False)
   ↓ fsync returns
spawner.spawn(task)         ← real side effect
   ↓
WALWriter.append(decision_type="task_spawn_confirmed", committed=True)
   ↓
WALWriter.mark_committed(intent_seq)  # remove from sidecar index
```

If the process crashes between the intent and the confirm, the intent
remains in the file with `committed=False` and the sidecar index points
to it. Recovery picks up there.

The sidecar index (`UncommittedIndex`) is a performance cache only - if
it is missing, truncated, or stale, recovery falls back to a full WAL
scan and rebuilds it (`wal.py`). Loss of the index never costs
correctness, only one slow boot.

---

## Recovery on restart

When `bernstein run` starts in a workdir that already contains `.sdd/`,
the recovery sequence is:

```text
1. Load durable backlog
   read .sdd/backlog/{open,claimed,closed}/*.yaml
   reconstruct in-memory task store

2. WALReplayEngine.scan_and_replay()
   a. WALRecovery.scan_all_uncommitted(sdd_dir, exclude_run_id=current)
      → list[(run_id, WALEntry)] where committed=False
   b. For each entry:
        if entry.decision_type in _SKIP_DECISION_TYPES → mark informational
        if (now - entry.timestamp) > MAX_REPLAY_AGE_S (1 h) → mark stale
        if IdempotencyStore.was_executed(key) → skip
        else                                     → replay_handler(entry)
   c. Append "wal_replay_completed" entry to current run's WAL

3. recover_stale_claimed_tasks()
   any task left in CLAIMED state by the dead orchestrator is reset to
   OPEN so a new agent can pick it up
   (core/tasks/task_store_core.py)

4. Begin tick loop
```

Source files: `src/bernstein/core/persistence/wal_replay.py`,
`src/bernstein/core/tasks/task_store_core.py`.

The idempotency store is a JSONL log at
`.sdd/runtime/wal/idempotency.jsonl` mapping `(decision_type,
entry_hash)` → executed. It survives crashes and is consulted before
every replay so the same intent is never executed twice
(`wal_replay.py`).

The 1-hour age cap (`_MAX_REPLAY_AGE_S` in `wal_replay.py`) means
WAL entries older than an hour are marked stale and skipped; the
operator must re-trigger the action manually if it is still wanted.
This prevents accidental replays of work the operator already
abandoned.

---

## CAS store and Merkle integrity

### Content-addressed artifacts

`CASStore` (`src/bernstein/core/persistence/cas_store.py`) deduplicates
arbitrary bytes by SHA-256 digest. Two agents that emit the same patch
or the same screenshot store one blob, not two. Layout mirrors git's
object store:

```text
.sdd/cas/{first-2-hex-chars}/{full-sha256-hex}
.sdd/cas/{first-2-hex-chars}/{full-sha256-hex}.meta.json
```

API (`cas_store.py`):

```python
store = CASStore(Path(".sdd/cas"))
digest = store.put(b"hello world", content_type="text/plain")
assert store.get(digest) == b"hello world"
```

Each blob has a `.meta.json` sidecar containing a `CASEntry`
(`cas_store.py`) with size, content type, creation timestamp,
and arbitrary user metadata. `CASStats` tracks how many `put()` calls
hit an existing blob (`dedup_saves`).

### Merkle seals over audit logs

`merkle.py` builds a binary Merkle tree over daily HMAC-chained audit
log files. Each file's last-line HMAC becomes a leaf; the root hash is
written to `.sdd/audit/merkle/seal-<ISO-timestamp>.json` and proves no
file was deleted, inserted, reordered, or tampered with between seals
(`src/bernstein/core/persistence/merkle.py`).

This is independent of the WAL hash chain - the WAL protects
orchestrator decisions, the Merkle seal protects compliance audit
evidence (auth events, policy denials, identity revocations).

---

## Persistence boundary

Durable (survive a crash, can be backed up):

- All YAML in `.sdd/backlog/{open,claimed,closed,issues}/`
- `.sdd/runtime/wal/*.wal.jsonl` and `idempotency.jsonl`
- `.sdd/metrics/*.jsonl` (cost ledger, cascade chain reports, file
  health, custom metrics)
- `.sdd/cas/**` (artifact blobs and `.meta.json` sidecars)
- `.sdd/audit/**` (HMAC-chained audit logs and Merkle seals)
- `.sdd/config/**` (MCP server catalog, model policy, etc.)

Ephemeral (regenerated by the next run):

- `.sdd/runtime/` PID files, sockets, agent signal queues
- `.sdd/runtime/logs/` per-session log fragments
- `.sdd/worktrees/` per-agent git worktrees
- `.sdd/runtime/wal/uncommitted.idx.json` (sidecar - rebuildable)

Never commit `.sdd/runtime/` or `.sdd/worktrees/`. The shipped
`.gitignore` excludes both.

---

## Code pointers

| Concern | File | Symbol |
|---------|------|--------|
| WAL writer (append, fsync, hash chain) | `src/bernstein/core/persistence/wal.py` | `WALWriter` |
| WAL reader + chain verification | `src/bernstein/core/persistence/wal.py` | `WALReader` |
| WAL recovery scan (all runs) | `src/bernstein/core/persistence/wal.py` | `WALRecovery` |
| Sidecar uncommitted index | `src/bernstein/core/persistence/wal.py` | `UncommittedIndex` |
| Replay engine | `src/bernstein/core/persistence/wal_replay.py` | `WALReplayEngine` |
| Idempotency store | `src/bernstein/core/persistence/wal_replay.py` | `IdempotencyStore` |
| Stale-claim recovery | `src/bernstein/core/tasks/task_store_core.py` | `recover_stale_claimed_tasks` |
| CAS store | `src/bernstein/core/persistence/cas_store.py` | `CASStore` |
| Merkle seal builder | `src/bernstein/core/persistence/merkle.py` | `file_leaf_hash`, tree builder |
| Backlog ingest (open → claimed) | `src/bernstein/core/orchestration/orchestrator_backlog.py` | `ingest_backlog`, `_claim_backlog_file` |
| Backlog sync (yaml ↔ task server) | `src/bernstein/core/persistence/sync.py` | `BacklogTask` |
| Disaster-recovery backup paths | `src/bernstein/core/persistence/disaster_recovery.py` | `_BACKUP_DIRS` |

Run-time helpers:

```bash
# verify WAL integrity
bernstein doctor

# back up the durable parts (used by `bernstein dr`)
bernstein dr backup --to ./backup.tar.gz

# inspect the replay log
jq -r 'select(.decision_type=="wal_replay_completed") | .inputs' \
  .sdd/runtime/wal/<run-id>.wal.jsonl
```

## Related

- `architecture/LIFECYCLE.md` - task and agent state machines that drive
  the WAL entries.
- `architecture/storage.md` - how `.sdd/` writes can be redirected to
  S3/GCS/R2 instead of the local filesystem.
- `operations/disaster-recovery.md` - the `bernstein dr` CLI.
