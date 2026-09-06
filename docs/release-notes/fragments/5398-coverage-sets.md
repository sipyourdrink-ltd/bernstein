## Structured coverage sets on the merge-admission receipt

The merge-admission receipt now carries a structured coverage view of the
verification work that backed an admission. `MergeAdmissionReceipt` exposes a
`VerificationScope` dataclass — `(oracle, checked, skipped, metadata)` — and
`emit_merge_receipt` computes `verified`, `unverified`, `skipped`, and a
content-addressed `coverage_set_hash` from the supplied scope set via the new
module-level helper `compute_coverage_sets()`. The new fields are written into
the binding block in sorted-key JSON, so a signed receipt stays
byte-reproducible. `MERGE_SCHEMA_VERSION` is bumped 1→2; `MergeAdmissionReceipt.from_dict`
still loads v1 receipts.

`emit_merge_receipt` gains two fail-closed gates that default to permissive
behavior for back-compat. Passing `required_oracle_kinds=` raises
`MissingOracleError` if any named oracle has no matching `VerificationScope`,
and passing `unverified_threshold=` raises `UnverifiedShareExceededError` when
the unverified share of the change set exceeds the limit. Both errors surface
in the receipt's `failure_reason` for the gate caller, so a refusal names the
scope set rather than collapsing to a bare false. The new public names
(`VerificationScope`, `MissingOracleError`, `UnverifiedShareExceededError`,
`compute_coverage_sets`, `MERGE_SCHEMA_VERSION`) are exported from
`bernstein.core.quality.merge_receipt`.

(#5398)
