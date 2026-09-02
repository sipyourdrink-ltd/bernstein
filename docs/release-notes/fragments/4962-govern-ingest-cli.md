## Anchor activity Bernstein did not schedule from a file or stdin (Issue #4962)

`bernstein governance ingest` is the first transport into the OTLP ingest
boundary. It reads OTLP/JSON spans reported by a runtime Bernstein did not
schedule, anchors them in the HMAC audit chain, and prints the signed receipt
covering the submission. The payload is parsed before any append, so a batch
the boundary rejects leaves the chain untouched, and `--source` is required
because the reporting identity is part of the signed receipt binding.

Ingest is now content-addressed: every record the boundary writes is addressed
by the SHA-256 of what was reported, scoped to the source and profile that
reported it. A retrying transport no longer turns one reported batch into
several -- a repeated span appends nothing, and a repeated batch returns the
receipt it was already anchored with. The seen-set is a query over the chain
rather than a side index, so it cannot drift from the chain it describes.

See `docs/operations/govern-ingest.md`.
