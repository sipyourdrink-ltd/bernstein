## Reclaim CAS storage with `bernstein gc cas`

Mark-and-sweep garbage collection for the content-addressed store. Referenced digests are collected from the durable roots — write-ahead log, snapshots, audit seals, lineage records and the backlog — so a reachable blob is preserved regardless of age. Supports `--days`, `--dry-run` and `--workdir`, and writes a prune receipt for every run (#4674).
