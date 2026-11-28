bernstein-verify-receipt

Standalone auditor CLI for Bernstein audit receipts.

Tool-call effect verification (`--effects-dir`) validates per-attestation
`toolcall.attestation -> toolcall.enforced_dispatch -> toolcall.effect` triples
and `retry_of` links under the uniqueness contract that JSON-RPC `request_id`
values are unique per `scope_id` (compared by raw JSON value across both initial
calls and retries). Outside-range `retry_of` links fail closed on receipts
starting at genesis.

See ../README.md for the full verify_cli documentation.
