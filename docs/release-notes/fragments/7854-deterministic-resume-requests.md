## Resume requests have deterministic IDs and real worker acknowledgement

`bernstein resume <task-id>` and `bernstein ledger resume <run-id>` now give each
local resume request a deterministic `request_id` based on the interrupted
attempt, not on a bumped counter or a ledger resume nonce. Repeating a command
for the same interrupted attempt reuses the same id without incrementing
`resume_count` again, while a newer checkpoint or ledger task transition mints
a distinct id.

The request's `queued -> claimed -> resumed` state survives worker restarts in
`.sdd/runtime/resume/<task-id>.requests.json`, beside the existing wake-up
signal. JSON output reports `runtime_acknowledged: true` only after a worker
records `resumed` for that exact request; a queued or interrupted-claimed
request stays `false`. Legacy signals are read as unacknowledged wake-up hints.
