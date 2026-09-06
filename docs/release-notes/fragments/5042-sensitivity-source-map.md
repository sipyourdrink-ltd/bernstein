## `bernstein lineage sensitivity` command reports effective data sensitivity

A new `bernstein lineage sensitivity <artifact|entry-hash>` command emits the effective sensitivity class of an artifact, the closure member that raised it, and the lineage path that reaches it. The `--json` flag formats output for scripts, and exit codes document both success (0) and verification failure (1). The lineage gate runs first: a failing log exits 1 without printing a class, and an unknown artifact or missing log reports the fail-closed class (#5042).
