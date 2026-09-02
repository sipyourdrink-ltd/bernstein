## Content-addressed anchors for review annotations

Adds `bernstein review-annotation derive/resolve`, a read-only CLI pair that
binds an operator's diff comment to the target bytes rather than a line
number: `derive` records the blob hash, the line range, and digests of the
target lines and the comment text; `resolve` reports where those bytes sit
now, or exits 1 with an `orphaned` reason code (`target_bytes_absent` /
`target_bytes_ambiguous`) instead of silently re-anchoring to whatever now
occupies the original offset. Neither command writes to disk. (#3456)
