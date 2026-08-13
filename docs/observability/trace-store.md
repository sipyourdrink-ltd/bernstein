# Content-addressed local trace store + viewer

Bernstein writes agent traces to `.sdd/traces/` as the orchestrator runs.
The **content-addressed trace store** layers a sha256-keyed archive plus a
small read-only FastAPI viewer on top of those traces, so operators get
the experience of a paid trace platform (search, timeline, replayable
JSON) without paying for one.

## Why

- Paid trace platforms charge per trace count, every month, forever.
- Local disk is free, never expires, and stays inside the operator's
  trust boundary.
- The same store doubles as the input to Bernstein's replay primitive.

## On-disk layout

```
.sdd/traces/
  blobs/
    <sha256[:2]>/
      <sha256>.jsonl.zst    # if zstandard is installed
      <sha256>.jsonl.gz     # otherwise (stdlib gzip)
  index.jsonl               # one TraceIndexEntry per line
```

- Each blob is the uncompressed trace bytes, compressed with whichever
  codec the writer had available. The filename always carries the codec
  via its extension, so readers can pick the right decoder regardless of
  which codec wrote it.
- `index.jsonl` carries the searchable metadata: `trace_id`, `task_id`,
  `sha256`, `byte_size`, `started_at`, `ended_at`, `model`, `cost_usd`,
  `codec`. The viewer reads this file; nothing else.
- The sha256 is the source of truth. `store.get()` rereads the blob,
  decompresses it, rehashes, and raises `CASIntegrityError` if the bytes
  no longer match the indexed digest - so the "index does not have to be
  trusted" property holds on the hot read path, not only when an operator
  runs `bernstein trace verify <id>` explicitly. The explicit verify
  command remains for an on-demand check that returns a boolean.

## Credential safety

Both live trace persistence and the content-addressed archive redact PII and
credential-shaped text before writing. This covers authorization headers,
known API-key prefixes, private-key blocks, and random-looking values assigned
to names containing `token`, `secret`, `password`, or `key`. Entropy by itself
does not trigger redaction, so ordinary content hashes, UUIDs, and base64
payloads remain intact.

For content-addressed blobs, redaction happens before sha256 calculation. The
index digest therefore identifies the exact safe bytes on disk, and a redacted
trace continues to pass `store.verify()`. Debug and ticket bundles copy these
already-redacted trace bytes; they cannot restore a credential removed at the
write boundary.

## CLI

| Command                                | What it does                                      |
| -------------------------------------- | ------------------------------------------------- |
| `bernstein trace show <task-id>`       | Pretty-print the live JSONL trace for a task.     |
| `bernstein trace <task-id>`            | Back-compat alias of `show`.                      |
| `bernstein trace serve --port 8765`    | Run the read-only viewer on `127.0.0.1`.          |
| `bernstein trace verify <trace-id>`    | Confirm the on-disk bytes match the indexed hash. |
| `bernstein trace reindex`              | Rebuild `index.jsonl` from the blob tree.         |

`trace serve` binds to loopback by default. To expose the viewer on a
specific interface (e.g. when running inside a sandbox), pass
`--bind 0.0.0.0` explicitly. Bernstein never opens it on a public
interface for you.

## Viewer endpoints

The FastAPI app served by `bernstein trace serve` exposes:

- `GET /` - HTML index with `task`, `model`, free-text filters.
- `GET /traces/<trace-id>` - pretty-printed JSON body.
- `GET /traces/<trace-id>/timeline` - HTML timeline of steps.
- `GET /api/traces` - JSON mirror of the filtered index.
- `GET /api/traces/<trace-id>` - JSON body of a single trace.

The HTML is intentionally minimal: HTML + inline CSS, no JS framework,
so screenshots are stable and the viewer survives in environments
without a bundler.

## Python API

The `ContentAddressedTraceStore` class is the public Python surface.

```python
from pathlib import Path
from bernstein.core.observability.trace_store import (
    ContentAddressedTraceStore,
    TraceMetadataHints,
)

store = ContentAddressedTraceStore(Path(".sdd/traces"))

# Store finalised trace bytes (idempotent).
entry = store.put(trace_bytes, hints=TraceMetadataHints(task_id="T-12"))

# Read back (verifies the bytes against the indexed digest by default),
# verify on demand, search.
raw = store.get(entry.trace_id)  # raises CASIntegrityError on a mismatch
assert store.verify(entry.trace_id)
results = store.search(task_id="T-12", model="sonnet")

# Rebuild the index after a crash or manual blob copy.
store.reindex()
```

## Out of scope (v1)

- Shipping traces to an external store. A separate ticket covers
  S3/GCS sinks via the existing `bernstein.core.storage` registry.
- Sharing traces across machines. The viewer is local-only.
- Full-text search inside trace bodies. `task_id` / `model` / free-text
  substring is enough in v1; we can plug in SQLite FTS5 later without
  changing the on-disk layout.
