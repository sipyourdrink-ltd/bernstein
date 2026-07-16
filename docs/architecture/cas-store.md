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
   on restart; large blob payloads referenced from WAL entries live in
   CAS rather than being copied inline.
3. **Audit evidence.** Tamper-evident audit logs use Merkle seals
   over CAS-resident content; the seal can be verified without re-reading
   the original blobs.
4. **Snapshot manifests.** `bernstein dr` snapshots reference CAS
   digests instead of duplicating large files into the snapshot dir.

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

- **Project reset / `bernstein cleanup`.** Removes the entire
  `.sdd/cas/` tree along with the rest of `.sdd/runtime/`. Use this if
  you've confirmed nothing in the durable state still points at CAS
  digests.
- **Disaster-recovery snapshot rotation.** Old `bernstein dr` snapshots
  drop their reference to CAS digests; a follow-up sweep can delete
  any blob whose digest is no longer referenced by any snapshot
  manifest.
- **Audit seal expiry.** When a Merkle seal ages out, the CAS entries
  it referenced can be deleted via `store.delete(digest)`.

Because `delete()` is explicit and per-digest, GC is essentially "find
the orphans and call delete on each one". A reference scan over WAL +
snapshots + audit seals produces the live set; everything in
`store.list_entries()` not in the live set is orphaned.

There is no built-in mark-and-sweep job. If your workload churns
through artifacts (e.g. heavy snapshot rotation), wire one up against
your reference set and run it as a periodic CLI step.

---

## Integrity: Merkle hash tree

CAS pairs naturally with the Merkle integrity layer at
`core/persistence/merkle.py`. The Merkle tree builds a binary hash tree
over a deterministically-ordered list of `(path, leaf_hash)` pairs and
publishes the root as a single SHA-256 string. For CAS entries:

- The **leaf hash** for a blob is its digest (which is its SHA-256 by
  construction).
- The **internal nodes** combine children with a domain-separated
  hash: `sha256("merkle:" + left + ":" + right)`
  (`merkle.py:_combine_hashes` at `merkle.py`).
- The **root** signs the entire CAS state at a point in time. A single
  root hash proves the contents of every leaf without re-reading them.

Seals are JSON files at `.sdd/audit/merkle/seal-<ISO-timestamp>.json`
and serve compliance evidence: a verifier rebuilds the tree from the
on-disk leaves, compares roots, and reports tamper.

If a CAS blob is corrupted (digest mismatch on read) or deleted
underneath the Merkle layer, the next seal verification will surface
the discrepancy.

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
| Merkle tree builder | `merkle.py` |
| State-persistence overview | `docs/architecture/state-persistence.md` |
