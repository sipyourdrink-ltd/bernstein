## A run archive's manifest can be anchored in an audit log

Per-file hashes in a run archive detect modification, but not authorship.
Someone who could edit a member could also recompute its hash and rewrite
`manifest.json`, and the archive verified clean.

`create_archive(..., audit_log=log)` now records the sha256 of the manifest it
wrote as a `run_archive.created` event in that HMAC-chained log.
`verify_archive(..., audit_log=log)` reports `attestation` beside the hash
result: `anchored` when a log whose chain verifies holds a record of this exact
manifest, `unattested` when it does not, and `unchecked` when no log was given.
A rewritten manifest still passes the hash check, and now reads `unattested`.
See `docs/observability/run-archive-retention.md` (#6258).
