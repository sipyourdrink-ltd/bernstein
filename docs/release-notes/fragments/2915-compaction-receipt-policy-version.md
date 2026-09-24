## Compaction receipts now bind policy versions into replay journals

Compaction receipts can now record `policy_version`, and replay-journal
compaction steps hash the same value so offline verification rejects a
receipt/journal policy-version mismatch. Legacy records without the field
remain verifiable as an empty version. This is a partial implementation;
journal-prefix folding and replay-time re-derivation of `post_sha256` remain
separate work. (#2915)
