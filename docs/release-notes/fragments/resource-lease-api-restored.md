## Tag-filtered resource leases are back in `resource_lease`

The tag-filtered lease API announced for #5128 (`ResourceRegistry`, `ResourceDeclaration`, `LeaseStore`, `Lease`, `claim()`, `release_all_held()`, `default_owner()`, `LeaseConflictError`, `NoMatchingResourceError`, `NoFreeResourceError`) shipped in 3.20.0 but a later refactor of `bernstein.core.sandbox.resource_lease` replaced the module with a single lock helper, so `import` of any of those names failed on `main`. The module is restored with its tests. Its `named_lock(store, name)` is the one released in 3.20.0; the `named_lock(repo_root, name)` variant from that refactor was never released, had no callers, and is removed.

## Lease reclaim is only done on proof of staleness, and only once

- A lease file that is empty or half-written is a holder between creating the file and writing its payload. It is never reclaimed while young. A file whose payload stays unreadable past `PAYLOAD_WRITE_GRACE_S` (30 s) is the leftover of a holder that died in that window and is now reclaimable, so it no longer blocks the resource forever.
- Reclaiming an expired lease, `keepalive()` and `release()` now run under a short advisory file lock. Before, several claimants that judged the same lease stale each unlinked and re-created it, so more than one could end up holding the resource and the later one deleted the earlier one's fresh lease. Exactly one claimant now takes it over; the rest get `LeaseConflictError`. Platforms without `fcntl` (Windows) keep the previous unguarded reclaim.
