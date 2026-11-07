# Disaster recovery

**Audience:** SREs planning a DR runbook for a Bernstein-managed
project - backups, restore drills, RPO/RTO targets, and the
forward-looking cross-region replication plan.

**What:** `bernstein dr` produces and consumes encrypted tarballs of the
durable `.sdd/` state. Combined with the WAL and checkpoint subsystem,
that backup is sufficient to bring a replacement orchestrator back to a
recent consistent state without rebuilding from a remote git remote.

**Why:** A Bernstein workspace is *the* source of truth for in-flight
backlog, claim ownership, audit chain, WAL hashes, cost ledger, and
cascade-router metrics. A lost workspace is not just lost code - it's
lost determinism and lost audit. Cross-link:
[State persistence](../architecture/state-persistence.md) for the durability
model that makes restore safe.

---

## Bernstein's recovery model (5-second mental model)

Two complementary mechanisms:

1. **Write-Ahead Log (WAL)** - every orchestrator decision (claim,
   spawn, complete, fail, merge) is appended to a hash-chained
   JSONL file with `fsync()` *before* the action runs.
   On startup, `wal_replay` finds entries marked uncommitted and
   re-executes them after consulting the idempotency store, so a
   crashed orchestrator wakes up consistent.
   Source: `src/bernstein/core/persistence/wal.py:1-15`,
   `src/bernstein/core/persistence/wal_replay.py:1-18`.
   File locks persisted in `.sdd/runtime/file_locks.json` by the crashed
   process's agents are released on the first tick, because no live
   session holds them; their retries do not wait out the lock TTL.
2. **Checkpointing** - periodic atomic snapshots of full orchestrator
   state (task graph + agent sessions + cost accumulator + WAL
   sequence position) at
   `.sdd/runtime/checkpoints/checkpoint-<id>.json` plus
   operator-visible "where we are" rows at `.sdd/sessions/<ts>-checkpoint.json`.
   Source: `src/bernstein/core/persistence/checkpoint.py:1-19`.

Backups bundle both, plus everything else listed in §"Backup contents"
below.

---

## `bernstein dr` group

Source: `src/bernstein/cli/commands/disaster_recovery_cmd.py`.

### `dr backup --to <path>`

Bundle the durable subset of `.sdd/` into a gzipped tarball. Optional
symmetric encryption via Fernet (PBKDF2-SHA256, 600k iterations).

```console
$ bernstein dr backup --to ./bernstein-backup-2026-05-04.tar.gz
Backing up .sdd to ./bernstein-backup-2026-05-04.tar.gz...
Backup complete!
  Path: ./bernstein-backup-2026-05-04.tar.gz
  Size: 18437298 bytes
  Files: 8421
  SHA256: 2f9a17b8c4e3d501...
```

Encrypted (recommended for off-site copy):

```console
$ bernstein dr backup --to ./bk.tar.gz --encrypt --password "$DR_PASSPHRASE"
```

Flags:

- `--to <path>` - required destination. Encrypted output gets `.enc`
  suffix appended automatically.
- `--encrypt` - wraps the tarball in Fernet ciphertext. Requires
  `--password` (a missing password is rejected because the random key
  would be unrecoverable -
  `src/bernstein/core/persistence/disaster_recovery.py:200-201`).
- `--password <str>` - passphrase fed through PBKDF2 with a fresh 16-byte
  salt prepended to the ciphertext.
- `--sdd <path>` - override the source `.sdd/` directory (defaults to
  `./.sdd`).

### `dr restore --from <path>`

Re-hydrate `.sdd/` from a backup. `--dry-run` lists the contained files
without extracting; use it as a verify step before pointing it at a
destination directory.

```console
$ bernstein dr restore --from ./bk.tar.gz --dry-run
Dry run - listing contents of ./bk.tar.gz:
  Files: 8421
  Source: ./bk.tar.gz
  SHA256: 2f9a17b8c4e3d501...

Files in backup:
manifest.json
backlog/open/task-...
runtime/wal/run-2026-05-04.wal.jsonl
...
```

Encrypted backup:

```console
$ bernstein dr restore --from ./bk.tar.gz.enc --decrypt --password "$DR_PASSPHRASE"
```

Flags:

- `--from <path>` - required source (`.tar.gz` or `.tar.gz.enc`).
- `--decrypt` + `--password` - only when the backup was encrypted.
- `--dry-run` - list contents and report SHA256, no writes.
- `--sdd <path>` - destination override.

### Verify

There is no dedicated `dr verify` subcommand today. The supported drill is:

1. `bernstein dr backup --to ./drill.tar.gz`
2. Move to a scratch dir.
3. `bernstein dr restore --from ./drill.tar.gz --dry-run`
4. Confirm `Files: ` matches the backup's printed `file_count`.
5. Optionally `bernstein dr restore --from ./drill.tar.gz --sdd /tmp/.sdd-restored`
   then `bernstein doctor --workspace /tmp` to revalidate.

For ad-hoc integrity checks, the WAL itself is hash-chained - running
the orchestrator against a restored workspace will fail loudly if the
chain is broken (`src/bernstein/core/persistence/wal.py:36-38`).

---

## Backup contents

Defined in `src/bernstein/core/persistence/disaster_recovery.py:46-123`.

**Included** (`_BACKUP_DIRS`):

| Path                              | Why                                              |
|-----------------------------------|--------------------------------------------------|
| `backlog/{open,done,closed,deferred,manual}` | Task claim state and history             |
| `metrics`                         | Cascade-router bandit history, SLO budgets       |
| `traces`                          | Distributed traces                               |
| `memory`                          | Persistent memory store                          |
| `sessions`                        | Operator-visible checkpoints                     |
| `decisions`                       | ADR-style decision logs                          |
| `docs`, `config`                  | In-tree docs and resolved config                 |
| `archive`, `agents`, `index`      | Agent registry + indexes                         |
| `caching`, `models`               | Cache state                                      |
| `audit`                           | HMAC-chained audit log                           |
| `runs`                            | Per-run reports                                  |
| `runtime/`                        | WAL, file locks, sessions, team state, task graph |

**Excluded** (`_EXCLUDE_DIRS` + `_EXCLUDE_PATTERNS`): rotated logs,
worktrees (regenerable), debug dumps, research caches, in-flight signals,
PID files, heartbeats, kill markers, `runtime/*.log`, `runtime/*.pid`,
`access.jsonl*`, `retrospective.md`, `summary.md`. The skip-list is the
boundary between "warm restart" and "everything you can rebuild on the
fly".

The tarball's root contains a `manifest.json` with the inclusion lists,
exclusion patterns, and `created_at` epoch
(`src/bernstein/core/persistence/disaster_recovery.py:207-223`).

---

## Restore procedure (step-by-step)

For a complete loss of the orchestrator host:

1. **Provision** a fresh node with the same Bernstein version. Mismatched
   versions can run, but tail your `bernstein doctor` output for
   compatibility warnings.
2. **Copy** the latest backup tarball to the new host (encrypted on the
   wire - these tarballs contain audit secrets and credential vault
   blobs).
3. **Decrypt** + extract:
   ```bash
   bernstein dr restore --from /backup/bernstein-latest.tar.gz.enc \
                        --decrypt --password "$(cat /vault/dr.pw)" \
                        --sdd /var/lib/bernstein/.sdd
   ```
4. **Restore secrets**: the credential vault is included
   (`runtime/`), but provider API keys live in environment variables
   (see [env-isolation.md](env-isolation.md)). Re-export those out of
   band.
5. **Start the orchestrator**: `bernstein start`. WAL replay runs
   automatically - every uncommitted entry from the previous instance
   replays through the idempotency store, so spawned-but-not-completed
   tasks finish correctly without duplicate side effects
   (`src/bernstein/core/persistence/wal_replay.py:1-18`).
6. **Verify**:
   - `bernstein status` - task counts match pre-incident.
   - `bernstein audit verify` - hash chain intact.
   - `bernstein verify --determinism <recovered-run> --baseline <pre-incident-run>`
     - asserts the recovered run's **decision trace** matches the
     pre-incident baseline (the WAL fingerprints are equal). Exits 0 on
     match, 2 on any divergence, and on a mismatch names the first diverging
     WAL entry (`seq` + decision type) from the hash chain. Pin a known-good
     digest instead with `--expect <fingerprint>` (compared constant-time).
     A green gate proves the WAL decision trace matched, **not** that on-disk
     artefacts are identical.
     - **On exit code 2 (divergence):** do not resume external triggers yet.
       1. Note the named diverging entry (`seq` + decision type) the gate
          printed, and archive both WALs
          (`.sdd/runtime/wal/<recovered-run>.wal.jsonl` and
          `<pre-incident-run>.wal.jsonl`) for root-cause analysis.
       2. Restore an earlier known-good backup (or re-run recovery from the
          same pre-incident baseline) so the recovered run replays from a
          clean starting point.
       3. Re-run the same
          `bernstein verify --determinism <recovered-run> --baseline <pre-incident-run>`
          (or pin the known-good digest with `--expect <fingerprint>`) and
          confirm exit 0 before resuming traffic.
       4. If divergence persists after a clean restore, open an incident with
          the archived WALs attached - the diverging `seq` is the first
          decision that differed and is the starting point for the
          investigation.
   - `bernstein dr backup --to /tmp/drill.tar.gz --dry-run` (sanity).
7. **Resume external triggers**: if any cron/CI/webhook was paused
   during failover, re-enable now.

For a partial loss (workspace corruption with the host alive):

1. `bernstein stop` to drain.
2. `mv .sdd .sdd.broken && bernstein dr restore --from /backup/...`.
3. `bernstein start`. WAL replay handles inconsistencies.

---

## RPO / RTO expectations

| Metric | Target                                                  | Notes |
|--------|---------------------------------------------------------|-------|
| **RPO** (Recovery Point Objective) | = backup cadence | Operator-set. Default cron: hourly snapshots in production, 6 h elsewhere. |
| **RTO** (Recovery Time Objective)  | < 15 min for the dr-restore step itself, plus your provisioning | Restore is a `tar -xzf` plus `fsync` - IO-bound, not CPU-bound. |

WAL fsync per entry guarantees zero-loss for *committed* state - every
decision is durable before it executes. The RPO gap is the time between
your last `dr backup` and the incident: WAL itself is included in the
backup, so a restored workspace replays uncommitted entries forward and
loses only the *bound but un-fsynced runtime telemetry* (heartbeats, log
tails, metrics counts). Cost ledger and audit chain are preserved.

---

## Cross-region considerations

The supported cross-region pattern today is scheduled backup + restore:

1. **Schedule** `bernstein dr backup --to s3://...` from cron (or your
   scheduler of choice) every 1 h. Push to a different region than the
   orchestrator host.
2. **Encrypt** with a passphrase you store in your secrets manager (not
   in the same region as the orchestrator host).
3. **Drill** restore quarterly into a scratch host. A backup you have
   not restored is not a backup.

Treat backup cadence as your RPO floor.

---

## Code pointers

- `src/bernstein/cli/commands/disaster_recovery_cmd.py` - CLI surface
- `src/bernstein/core/persistence/disaster_recovery.py:1-22` - design rationale + usage
- `src/bernstein/core/persistence/disaster_recovery.py:46-123` - included/excluded paths
- `src/bernstein/core/persistence/disaster_recovery.py:139-174` - Fernet/PBKDF2 crypto
- `src/bernstein/core/persistence/disaster_recovery.py:177-275` - `backup_sdd`
- `src/bernstein/core/persistence/disaster_recovery.py:278-366` - `restore_sdd` (with `filter="data"` traversal guard)
- `src/bernstein/core/persistence/wal.py:1-67` - WAL writer, hash-chain, fsync invariants
- `src/bernstein/core/persistence/wal_replay.py:1-78` - replay pipeline + `IdempotencyStore`
- `src/bernstein/core/persistence/checkpoint.py:1-79` - `Checkpoint` (atomic) + `PartialState` (operator)
