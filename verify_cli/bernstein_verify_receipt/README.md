bernstein-verify-receipt

Standalone auditor CLI for Bernstein audit receipts.

Tool-call effect verification (`--effects-dir`) validates per-attestation
`toolcall.attestation -> toolcall.enforced_dispatch -> toolcall.effect` triples,
stream-hashes `<intent_sha256>.effect` payloads up to 16 MiB, and validates
`retry_of` links under the contract that JSON-RPC `request_id` values are
present, non-blank, and unique per `scope_id` (compared by raw JSON value across
both initial calls and retries). Outside-range `retry_of` links fail closed on
receipts starting at genesis.

See ../README.md for the full verify_cli documentation.
