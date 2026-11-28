## Offline audit receipt verifiers check tool-call effect triples and retry links

`verify-audit-receipt` (`tools/verify_audit_receipt.py`) and the standalone
`bernstein-verify-receipt` package (`verify_cli/bernstein_verify_receipt/`) now
validate per-attestation `toolcall.attestation -> toolcall.enforced_dispatch ->
toolcall.effect` triples and `retry_of` links without importing `bernstein.*`.
When `--effects-dir` is passed (backed by `NativeToolCallEvidenceProvider`'s
optional `effects_dir` payload store), both verifiers stream-hash
`<intent_sha256>.effect` (bounded at 16 MiB) to recompute every recorded
`effect_digest` and fail the effect leg on any missing, non-regular, oversized,
or tampered response or diff. Pre-effect chains and dispatched calls
without an effect row still verify and report `effect unobserved` rather than
success. `ToolCallIntent` now accepts an optional `retry_of` intent digest
(wired on `MCPGateway`'s x402 retry path with a distinct intent `request_id`),
preserving one-attestation-one-dispatch semantics (#6270).
