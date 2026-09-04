## A per-item batch ledger, and a daily cap derived from it

`bernstein.core.persistence.batch_ledger.BatchLedger` records each item a
flat batch completes, so a resumed run skips what is already done and a
crash costs the item in flight and nothing before it. `WorkLedger`
resumes a task graph by replaying a chain; a batch asks two different
questions — "have I already done this one" and "how many have I done
today" — and nothing mapped an entity id to its last success.

The daily cap is derived from the ledger's own entries rather than a
counter in memory, so it survives a restart — which is exactly when an
operator most wants it to hold. `record(..., cap=N)` raises
`DailyCapReached` rather than returning false: the cap holding is not a
failure of the item. The check and the append are one section, so two
callers cannot both take the last slot.

Entries are hash-chained with the same `compute_entry_hash` the work
ledger uses and appended with `O_APPEND` + `fsync`, so an item treated as
done is on disk. A torn final line from a killed process is dropped
rather than failing the load; a break anywhere else is reported.
`compact()` applies a retention window, re-deriving the chain over the
survivors and writing through a scratch sibling (#5126).
