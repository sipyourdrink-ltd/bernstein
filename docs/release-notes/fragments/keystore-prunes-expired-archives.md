## Rotation deletes the archives its grace window has closed

`AgentCardKeystore.rotate()` moved the retired keypair under `archive/<utc-stamp>/` and nothing ever removed it. `list_archived` only *skipped* entries past the grace window - its docstring said they "may be GC'd by the operator out-of-band" - so every rotation left another directory behind permanently. The archived directory holds the retired **private** key beside the public one, so this was not only unbounded disk growth: it was an unbounded set of retired signing keys kept long after they stopped being published. Rotation now prunes them, using the same cutoff expression `list_archived` uses so the two cannot disagree about which entries are live. An archive whose rotation timestamp cannot be read is left alone rather than deleted.

## Two rotations in the same second no longer overwrite each other

The archive directory name is second-resolution, so two rotations inside one second reused the same directory and `Path.replace` overwrote the keypair already archived there. The write succeeds, so nothing surfaced - a verifier still inside its grace window simply lost the key it was cached on. Colliding names now take a `-N` suffix, which the directory-name date fallback strips (#5104).
