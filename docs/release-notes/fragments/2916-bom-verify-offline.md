## `bernstein bom verify --from-lineage` re-derives and fails closed

`bom verify` only checked a document's shape, so an auditor could not tell
a faithful projection from a hand-edited claim without separately walking
the lineage chain. `--from-lineage --run <id>` now resolves every component
hash against the run's spine and compares the document's head anchor to the
chain head, naming the line item when a hash resolves to no verifying entry.
