## Freshness-gated inventory and report reads

Inventory and report reads in `govern` are now freshness-gated: reads of artifacts
older than their configured TTL trigger the producer and block until a terminal state
is reached before serving. Concurrent stale reads collapse onto exactly one producer
run to prevent thundering-herd stampedes, and `--no-wait` returns the stale artifact
marked `is_stale: true` in the response body (#5130).
