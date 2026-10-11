## Incremental audit verification reports a sealed segment that shrank or vanished

`AuditLog.verify_incremental()` trusts a segment's sealed prefix through the
signed checkpoint, but used the pin only as a shortcut. A segment cut back to
a record boundary inside its pin, or deleted outright, leaves a chain that
still walks clean, so the run passed it. With a hash tile present, a cut-back
segment was reported, but as a fault in the tile, which was correct. Without
one (the orchestrator's shutdown seal publishes none), or when the segment
was deleted, the run reported a clean history.

The verifier now reports a pinned segment that is gone, shorter than its
pin, or whose pinned prefix changed under a chain that still verifies. It
names the segment with the wording `bernstein audit verify`'s checkpoint
pillar uses, honours an `ack-tear` acknowledgement for that checkpoint the
same way, and no longer blames the tile. An audit directory emptied of
segments but holding a checkpoint is no longer reported as a clean empty
history. Records appended after the newest seal are pinned by nothing, so
removing them remains undetectable from the directory alone (#3160).
