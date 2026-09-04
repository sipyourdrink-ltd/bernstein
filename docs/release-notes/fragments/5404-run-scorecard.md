## Per-run scorecard (`bernstein runs scorecard`)

`bernstein runs scorecard <run-id>` derives a deterministic,
content-addressed projection of a run's work ledger and writes
`<sha256>.json` under `.sdd/runs/<run-id>/scorecard/`. The same
fields `bernstein runs report` already classifies (`run_id`,
`branch`, `outcome`, `evidence`, `started_at`, `ended_at`) plus a
small set of counters (`steps`, `tasks_total`, `tasks_completed`,
`tasks_failed`, `tasks_started`, `cost_usd`, `host`,
`parent_run_id`, `attempt_count`, `elapsed_seconds`). `--verify`
re-derives the scorecard from the live ledger and names the
diverging field on mismatch; `--json` prints the canonical content.
The artifact name is the SHA-256 of the canonical content, so
re-running over the same ledger overwrites the identical file
(idempotent by construction) (#5404).
