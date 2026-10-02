## Incremental audit verification trusts the signed checkpoint, not the tile

`AuditLog.verify_incremental()` trusted a segment whenever its bytes hashed
to the `content_sha256` in its hash tile, and then skipped walking it. A tile
is unsigned, so editing a record and rewriting the tile to match passed the
incremental verify while `verify()` failed. The same path adopted the
`end_hmac` of a trusted tile as the chain head, but `bernstein audit seal`
never writes one, so after an ordinary seal and one append the incremental
verify reported the next record as a broken chain.

Trust now comes from the newest signed checkpoint
(`checkpoints/latest.json`, falling back to the ledger). A segment prefix is
trusted only when its bytes reproduce the signed leaf hash and its first
record links to the head the walk has reached, and only the records appended
after the pin are walked, with findings named by their line in the segment
exactly as `verify()` names them. `IncrementalVerifyReport.records_walked`
reports that work: N appended records after a seal walk N records.

Each published tile is checked byte for byte against the tile the seal would
write for the authenticated bytes, so a corrupt tile is reported as
`tiles/<segment>.tile: ...`. Tiles are now rendered with LF line endings on
every platform and published with the same temp-file, fsync and rename
helper as the checkpoint pointer, so identical directories produce
identical tile bytes on Windows and Linux; CRLF tiles from earlier releases
still verify (#3160).
