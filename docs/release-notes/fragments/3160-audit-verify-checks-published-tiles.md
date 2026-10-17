## `bernstein audit verify` checks the published hash tiles

`audit seal` publishes a hash tile beside each sealed segment, but
`bernstein audit verify` never read them: a tile with a flipped byte, or
truncated to half its length, verified with exit 0 and was never mentioned.
Only the library's `AuditLog.verify_incremental()` checked tiles.

`audit verify` gains a **Hash Tiles** pillar. Each tile is checked byte for
byte against the bytes of its segment once those are authenticated, and a
damaged, truncated or unreadable tile fails verify naming
`tiles/<segment>.tile`, including under `--hmac-only` and `--merkle-only`.
A tile beside a segment that already fails is not judged, and a layout with
no tiles passes. The pillar is read-only like the rest of the command:
`verify_incremental(record_tile_reads=False)` skips the tile-read counter.

The incremental verifier now reads every tile before the segments it
describes. Read the other way round, a seal that landed between reading a
segment and reading its tile published a tile for bytes the run never saw,
and an honest layout was reported as a damaged tile (#3160).
