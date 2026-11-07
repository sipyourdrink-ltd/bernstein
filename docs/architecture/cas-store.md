# CAS Store

**Where do duplicated artifact bytes go to die?**

Bernstein stores agent outputs - file dumps, log fragments, structured
state - in a content-addressable store keyed by the SHA-256 digest of
their bytes. Identical content produces identical keys, so writing the
same payload twice costs zero extra bytes on disk. The layout is a
flat-file knock-off of git's object store, living under `.sdd/cas/`.

If you only have time for one sentence: **the CAS store is a directory
of SHA-256-keyed blobs at `.sdd/cas/<xx>/<sha256>`, with a
`.meta.json` sidecar per blob, and `put()` is a no-op on duplicates**.
The rest of this page covers usage, on-disk layout, GC, and Merkle
integrity.

---

## What content-addressed storage is

A normal file store is *named*: you write `report.json` and look it up
by name. Two writes of the same bytes use twice the disk.

A content-addressed store is *keyed by hash*: you write `report.json`
and the store hands you back a 64-char hex digest. Look-up is by digest,
not name. Identical bytes produce the same digest, so a second write of
the same content returns the existing digest without storing anything
new. Properties that follow:

- **Automatic dedup.** Two agents emitting the same artifact share one
  blob.
- **Tamper evidence.** Every `get()` re-hashes the bytes it read and
  compares them to the requested digest; a mismatch raises
  `CASIntegrityError` (corruption, full stop) rather than handing back
  the wrong bytes. Pair with a Merkle tree to prove the whole store
  hasn't been tampered with.
- **Cheap snapshots.** A "manifest" (list of digests) is a tiny pointer
  into the blob store; you can move snapshots around without copying
  blob content.
- **Immutable by construction.** You cannot rewrite a blob without its
  digest changing. The only mutation is delete.

---

## Where Bernstein uses CAS

The store is a primitive other persistence subsystems can plug into.
Today the wired-in consumer surface is small (the module is one of the
"undocumented surprises" surfaced by code-surface inventory) - `CASStore`
is constructed against `.sdd/cas/` and exposed for:

1. **Artifact dedup.** Agent outputs that two or more runs would
   produce identically (lockfile dumps, generated configs, large
   prompt-cache snapshots) get hashed into CAS so disk usage tracks
   *unique* bytes, not write count.
2. **Replay state.** WAL replay (`wal_replay.py`) restores the task graph
   on restart; digests referenced from WAL `inputs` / `output` fields are
   kept alive by the GC.
3. **Attachments and attestations.** `core/agents/attachment_dispatch.py`
   and the micro-VM sandbox backend (`core/sandbox/backends/microvm.py`)
   write their bytes into a `CASStore` under `.sdd/cas/`.
4. **GC roots.** `bernstein gc cas` treats WAL entries, audit Merkle
   seals, lineage spines, and backlog tasks as durable roots that keep a
   digest alive (see below). Session snapshots are not scanned as roots.

`bernstein.core.persistence.cas_store` is the only module that touches
the on-disk layout - every other consumer goes through `put()` /
`get()` / `has()` / `delete()` so the store layout is free to evolve.

---

## On-disk layout

`.sdd/cas/` mirrors git's object database:

```text
.sdd/cas/
├── 0a/
│   ├── 0a3f4c2e8b1d... 5c                  ← blob (raw bytes)
│   └── 0a3f4c2e8b1d... 5c.meta.json        ← sidecar metadata
├── 1f/
│   ├── 1f99ab...                           ← blob
│   └── 1f99ab... .meta.json                ← sidecar
└── ...
```

Sharded by the **first two hex characters** of the digest
(`_shard_dir` at `cas_store.py`) so no single directory holds
millions of files. With a uniform SHA-256 distribution that's 256
possible shards - plenty of headroom.

Each blob has a JSON sidecar (`<digest>.meta.json`) containing the
`CASEntry` fields:

| Field | Type | Meaning |
|-------|------|---------|
| `digest` | string | SHA-256 hex (matches the filename). |
| `size_bytes` | int | Content length. |
| `created_at` | float | Unix timestamp of first insertion. |
| `content_type` | string | MIME-style tag (`text/x-python`, `application/json`, ...). |
| `metadata` | dict | Arbitrary user-supplied metadata. |

Source: `CASEntry` at `cas_store.py`.

`has(digest)` requires both blob and sidecar to exist
(`cas_store.py`); a half-written entry is reported absent so
recovery code can treat it as such.

---

## API

`CASStore` is a thin class around the directory layout
(`cas_store.py`):

```python
from pathlib import Path
from bernstein.core.persistence.cas_store import CASStore, put_text

store = CASStore(Path(".sdd/cas"))

digest = store.put(b"hello world", content_type="text/plain")
assert store.get(digest) == b"hello world"
assert store.has(digest)

# Convenience wrappers for the common cases:
digest = put_text(store, "some text", metadata={"role": "qa"})
```

Public methods:

- `put(content, content_type, metadata)` → digest. No-op if digest
  already exists (`_dedup_saves` counter increments).
- `get(digest, *, verify=True)` → bytes or `None`. Validates the digest
  format first to prevent path traversal (`_validate_digest`), then
  re-hashes the stored bytes and raises `CASIntegrityError` if they do
  not match the requested digest. A missing blob still returns `None`.
  Pass `verify=False` only on hot paths that have already verified the
  content upstream - the opt-out re-opens the integrity hole for that
  call and must be used deliberately.
- `has(digest)` → bool.
- `delete(digest)` → bool. Removes blob + sidecar; cleans up empty
  shard directories.
- `get_entry(digest)` → `CASEntry` or `None`.
- `list_entries()` → list of all entries, sorted by `created_at`.
- `stats()` → `CASStats(total_entries, total_bytes, dedup_saves)` for
  dashboards.

Convenience helpers in the same module:

- `put_file(store, path, metadata)` - read a file, guess `content_type`
  from the suffix, store with `source_file` in metadata.
- `put_text(store, text, metadata)` - UTF-8 encode, store as
  `text/plain`.

Digest validation is regex-based (`_HEX_RE = r"\A[0-9a-f]{64}\Z"`) so
a malformed digest never reaches `Path` and a directory-traversal `..`
can't sneak through.

---

## Garbage collection

CAS entries are **never automatically deleted by the store itself**.
Pruning is the responsibility of higher-level subsystems that know
which digests are still referenced. Today GC happens during these
operations:

- **Safe incremental pruning / `bernstein gc cas`.** Performs a
  mark-and-sweep over the CAS store, deleting unreferenced blobs
  older than a configured retention window (default 30 days,
  `cas_retention_days` in `core/defaults.py`). Uses
  reachability analysis over durable roots (WAL, audit seals, lineage
  spines, backlog tasks) and preserves referenced or young entries. Only
  a sweep that deletes at least one entry writes a prune receipt to the
  CAS store for verification; a `--dry-run`, a run with nothing to
  delete, or a run refused because a root could not be read writes no
  receipt. Refuses to delete anything when a root could not be
  read. Recommended for regular CAS maintenance.
- **Manual deletion.** Anything that knows a digest is no longer
  referenced (for example an expired audit seal) can call
  `store.delete(digest)`.

The `bernstein gc cas` command is the safe, incremental approach to
CAS maintenance, designed to minimize the risk of accidentally deleting
referenced content while still reclaiming storage from unused artifacts.

## `bernstein gc cas`

### When to use

Use `bernstein gc cas` for routine CAS maintenance to reclaim storage
from unreferenced artifacts while protecting referenced content.

### Safety guarantees

The command provides several safety guarantees:

1. **Retention window** – By default, only blobs older than 30 days
   are eligible for deletion (`cas_retention_days`). You can adjust this
   with `--days N`.

2. **Reachability analysis** – The command scans durable roots to
   determine what is still referenced:
   - WAL entries (`.sdd/runtime/wal/*.wal.jsonl`)
   - Audit Merkle seals (`.sdd/audit/merkle/seal-*.json`)
   - Lineage spines (`.sdd/lineage/*/spine.jsonl`)
   - Backlog tasks (`.sdd/backlog/{open,done}/{*.yaml,*.yml}`)

   Only blobs whose digests are NOT in this referenced set are
   candidates for deletion.

3. **Preserve young entries** – Blobs created within the retention
   window are preserved even if unreferenced.

4. **Prune receipts** – A `gc cas` sweep that deletes at least one
   entry writes a prune receipt to the CAS store itself (a `--dry-run`,
   a run with nothing to delete, or a refused run writes none). This
   receipt documents what was deleted and serves as verification that
   the operation completed as expected.

### Command usage

```bash
# Show help
bernstein gc cas --help

# Dry-run: see what would be deleted without actually deleting
bernstein gc cas --dry-run

# Run GC with 60-day retention
bernstein gc cas --days 60

# Force immediate cleanup (dangerous: may delete young artifacts)
bernstein gc cas --days 0
```

### Key options

- `--days N` – Delete blobs older than N days (default: the configured retention window, 30; 0 = immediate)
- `--dry-run` – Preview deletions without modifying the store
- `--workdir PATH` – Root directory containing `.sdd/` (default `.`)
- `--yes` – Skip the confirmation prompt

### What happens during GC

1. **Mark phase** – Collect all referenced digests from durable roots
2. **Sweep phase** – Delete unreferenced blobs older than retention window
3. **Receipt** – Write a prune receipt documenting the operation, only when the sweep deleted at least one entry

### Common scenarios

**Storage recovery after artifact churn**

If your workflow generates many temporary artifacts (e.g. build caches),
run periodic GC to prevent CAS store growth:

```bash
# Weekly cleanup (crontab entry)
bernstein gc cas --days 7 --yes
```

### Relation to `bernstein cleanup`

`bernstein cleanup` removes Bernstein worktrees that no longer back active
tasks; it does not touch `.sdd/cas/`. Use `bernstein gc cas` for CAS
maintenance.

Because `delete()` is explicit and per-digest, GC is essentially "find
the orphans and call delete on each one". The reference scan over WAL,
audit seals, lineage spines, and backlog tasks produces the live
set; everything in `store.list_entries()` not in the live set (and older
than the retention window) is orphaned.

---

## Integrity: Merkle hash tree

The Merkle integrity layer at `core/persistence/merkle.py` seals the daily
HMAC-chained audit log files, not CAS blobs. It builds a binary hash tree
over a deterministically-ordered list of `(path, leaf_hash)` pairs and
publishes the root as a single SHA-256 string:

- The **leaf hash** for a file binds its whole canonical content, hashed
  with a `0x00` domain tag (`_leaf_digest`).
- The **internal nodes** combine children with a `0x01` domain tag,
  `sha256(0x01 || left || right)` (`_combine_internal`); a lone node at an
  odd level is promoted unchanged. Seals recorded under the earlier
  scheme (v1, `sha256("merkle:" + left + ":" + right)`) still verify
  (`_combine_hashes`).
- The **root** signs the whole audit-log state at a point in time.

Seals are JSON files at `.sdd/audit/merkle/seal-<ISO-timestamp>.json`
and serve compliance evidence: a verifier rebuilds the tree from the
on-disk leaves, compares roots, and reports tamper.

The two layers meet in GC: a CAS digest mentioned in a seal file counts as
referenced, so `bernstein gc cas` will not delete it. Integrity of the blobs
themselves comes from the re-hash on every `get()`.

---

## Cross-links

- See [`state-persistence.md`](state-persistence.md) for the full
  `.sdd/` layout and where CAS sits relative to WAL, audit logs, and
  the backlog. The `state-persistence` doc lists CAS as one of the
  "durable" surfaces - meaning it survives a restart and you should
  *not* gitignore it if you depend on artifact dedup across runs.

- See [`warm-pool.md`](warm-pool.md) for the orthogonal optimisation
  on the spawn path (the warm pool dedups *processes*; CAS dedups
  *bytes*).

---

## Code pointers

| Concern | File |
|---------|------|
| Store implementation (put/get/has/delete) | `src/bernstein/core/persistence/cas_store.py` |
| `CASEntry` / `CASStats` data classes | `cas_store.py` |
| `put_file` / `put_text` helpers | `cas_store.py` |
| Digest validation (path-traversal guard) | `_HEX_RE`, `_validate_digest` at `cas_store.py` |
| Shard layout | `_shard_dir`, `_blob_path`, `_meta_path` at `cas_store.py` |
| Merkle leaf hashing | `src/bernstein/core/persistence/merkle.py` |
| Merkle tree builder | `build_merkle_tree` at `merkle.py` |
| CAS garbage collection | `src/bernstein/core/persistence/cas_gc.py` |
| State-persistence overview | `docs/architecture/state-persistence.md` |
