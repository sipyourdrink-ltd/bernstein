## `bernstein memory show` prints a memory chain's current state

A new `bernstein memory show --scope <s> --namespace <ns>` command folds a
memory chain to its live claims -- every write in append order, minus the
ones a tombstone has forgotten -- and prints each one with the run, step,
actor and `entry_hash` it came from, instead of an operator reading the
chain's JSONL by hand and subtracting tombstones manually. `--json` emits
the canonical fold bytes verbatim: two independent readers over the same
chain produce them byte for byte, so the current state can be hashed or
diffed rather than re-derived. (#2914)
