# Artifact Storage Sinks

Bernstein persists its working state under `.sdd/`: the WAL, HMAC
audit logs, runtime state, task outputs, cost ledger, and metrics
dumps. On a developer laptop that directory lives on a local disk
and the story is simple. On ephemeral compute - CI runners, Kubernetes
pods, cloud sandboxes - the host can disappear between orchestrator
restarts, taking the recovery state with it.

The storage package decouples `.sdd/` persistence from the local
filesystem so artifacts can stream to S3, Google Cloud Storage,
Azure Blob, Cloudflare R2, or a custom plugin while the orchestrator
logic is unchanged.

## Protocol

Every sink implements the async [`ArtifactSink`][sink] protocol:

```python
class ArtifactSink(Protocol):
    name: str

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None: ...
    async def read(self, key: str) -> bytes: ...
    async def list(self, prefix: str) -> list[str]: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def stat(self, key: str) -> ArtifactStat: ...
    async def close(self) -> None: ...
```

Keys are forward-slash-delimited logical paths such as
`runtime/wal/run-123.wal.jsonl`. Implementations map them to whatever
native addressing their backend uses (object-store keys, filesystem
paths, ...). The helper module `bernstein.core.storage.keys`
centralises the canonical key layout so no caller hand-constructs a
path.

[sink]: https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/storage/sink.py

## First-party sinks

| Name         | Extra         | SDK                        | Ships in                      |
| ------------ | ------------- | -------------------------- | ----------------------------- |
| `local_fs`   | - (always on) | stdlib                     | `bernstein` core              |
| `s3`         | `bernstein[s3]`    | `boto3`                    | optional extra                |
| `gcs`        | `bernstein[gcs]`   | `google-cloud-storage`     | optional extra                |
| `azure_blob` | `bernstein[azure]` | `azure-storage-blob`       | optional extra                |
| `r2`         | `bernstein[r2]`    | `boto3` (R2 is S3-compatible) | optional extra             |

All five pass the shared `ArtifactSinkConformance` test suite
(`src/bernstein/core/storage/conformance.py`). The unit suite runs it
only against `LocalFsSink`; the cloud sinks run the same suite in the
integration folder behind emulator-availability gates.

## Durability: `BufferedSink`

The WAL crash-safety contract requires a `durable=True` write to be on
stable storage before `write` returns. A synchronous PUT to S3 on every
WAL append would tank throughput to the network round-trip time; a
pure-async mirror would break the crash invariant.

`BufferedSink` splits the difference:

```
WAL.append() ──▶ LocalFsSink.write(durable=True)   ← synchronous fsync
                        │
                        └──▶ queue entry
                                 │
                                 └──▶ [bg] RemoteSink.write(durable=True)
```

1. **Local first, synchronously.** The caller's write is committed to
   the local `.sdd/` with the full `fsync` semantics from
   `atomic_write.write_atomic_bytes`. If the orchestrator dies after
   `write` returns, every line is already on the OS page cache barrier.
2. **Remote next, asynchronously.** The same payload is queued for a
   best-effort mirror to the configured remote sink. The queue is
   bounded so a slow remote applies back-pressure rather than growing
   unbounded.
3. **Graceful shutdown.** `close()` blocks until every pending mirror
   has ACKed or failed. The orchestrator calls this on normal exit so
   nothing is lost.

Reads prefer the remote sink - that's the crash-recovery path where
the ephemeral local disk may be empty. They fall back to local when
the remote is unreachable or doesn't have the key (e.g. the mirror is
still pending).

## Sandbox integration

`WorkspaceManifest` carries a tuple of `ArtifactMount` entries
(`S3Mount`, `GCSMount`, `AzureBlobMount`, `R2Mount`). Cloud sandbox
backends (docker, e2b, modal) translate these into provider-native
filesystem bindings
(`rclone mount` for S3/R2, `gcsfuse` for GCS, `blobfuse2` for Azure)
so agent writes to the mount path stream straight into the
orchestrator's artifact sink. The `worktree` backend ignores the
field - everything already lives on the host filesystem.

## Credential handling

Each sink picks credentials up from environment variables with
explicit-config overrides. The exhaustive list of env vars consumed
by the first-party sinks lives in
`bernstein.core.storage.credential_scoping.STORAGE_CREDENTIAL_ENV_VARS`.

The default agent-spawn path uses whitelist-based env filtering
(`bernstein.adapters.env_isolation.build_filtered_env`) so sink
credentials are already stripped before any agent subprocess sees
them. The `scrub_env` helper is available for spawner paths that
bypass the whitelist.

## Trade-offs per provider

| Provider           | Write latency (p50)        | Durability               | Cost posture          | Notes                                                |
| ------------------ | -------------------------- | ------------------------ | --------------------- | ---------------------------------------------------- |
| `local_fs`         | sub-millisecond (fsync)    | local disk only          | free                  | Default. Zero network dependency.                    |
| `s3`               | 20–80 ms (same region)     | 99.999999999% (11 nines) | per-GB storage + PUT  | Widely available; best-documented durability guarantee. |
| `gcs`              | 30–100 ms                  | 99.999999999%            | per-GB + class-A ops  | Good integration with GKE workload identity.         |
| `azure_blob`       | 40–120 ms                  | 99.999999999%            | per-GB + transactions | LRS/ZRS/GRS replication tiers.                       |
| `r2`               | 50–150 ms (global)         | 99.999999999% (claimed)  | zero egress           | Best egress economics; no AWS-ecosystem lock-in.     |

Always pair a remote sink with `BufferedSink` in production. The
synchronous write path is then bounded by local fsync latency (~1 ms)
while cloud durability catches up in the background.

## Registering custom sinks

Third-party packages add sinks via the `bernstein.storage_sinks`
entry-point group. Example in `pyproject.toml`:

```toml
[project.entry-points."bernstein.storage_sinks"]
my_sink = "mypkg.storage:MyArtifactSink"
```

At import time `bernstein.core.storage.registry` loads every
entry-point in the group. Classes are instantiated lazily on first
`get_sink("my_sink")`; instances are cached across the process
lifetime.

## Observability

Each sink operation emits Prometheus metrics through the existing
`bernstein.core.observability` stack:

- `storage_write_total{sink, durable}`
- `storage_write_duration_seconds{sink}`
- `storage_read_bytes_total{sink}`
- `storage_buffer_pending_writes{sink}` (BufferedSink)
- `storage_buffer_lag_seconds{sink}` (BufferedSink)

`BufferedSink.stats()` exposes the raw counters as a
`BufferedSinkStats` dataclass for test assertions and ad-hoc
debugging.
