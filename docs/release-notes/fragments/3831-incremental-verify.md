## Incremental `audit verify` reads only the changed tiles

`bernstein audit verify` now trusts previously sealed segments by hash. Each
segment's hash tile (`<segment>.tile`) records the plain `SHA-256` of the
sealed byte prefix; the verifier recomputes that hash against the on-disk
bytes and only re-walks segments whose content no longer matches. A tile
that does not match (or has no tile, or a non-string `content_sha256`) is
re-read and reported, never silently skipped because it was seen before.
The "already verified up to here" marker lives at
`<audit_dir>/.tiles-read.json`; an operator can force a full re-verify by
removing it (which costs time, never correctness) or by calling
`AuditLog.force_full_verify`. The new `verify_incremental` method returns
an `IncrementalVerifyReport` carrying the tile-read count so the
measurement is observable, not just a hidden cache. (#3831)
