## Six more atomic writers go through the crash-safe path

Follow-up to the trigger-state and fleet-context conversion. Six further `_atomic_write` helpers renamed a temporary into place without ever calling `fsync`, so the rename could be durable while the bytes behind it were not: the MCP catalog user config, the review-responder dedup state, the template-compression backup, the tunnel registry, the plugin pin manifest, and the evolution upgrade applicator.

The applicator had two more problems. It published with `Path.rename`, which raises `FileExistsError` on Windows when the destination exists, so rewriting an upgrade proposal failed outright there rather than replacing the file. And it opened the temporary with no encoding, so the YAML was written in the host locale rather than UTF-8. The MCP catalog config had the same encoding issue via `Path.write_text`.

The template-compression case is worth calling out because it verifies its own write by reading the file back and comparing a SHA-256. Without an `fsync` that readback came from the page cache, so it proved the write call had run rather than that a backup existed on disk to be read after a crash.

All six now call `write_atomic_bytes` / `write_atomic_text`, and two guards keep the next copy honest: no `*atomic*` helper may publish with `rename`, and every one must either `fsync` or delegate.
