## `bernstein verify coverage` subcommand reports gate/oracle coverage for a merge

`bernstein verify coverage <head-sha>` loads the merge admission receipt for
`HEAD_SHA` and grades the four structural-coverage fields (`gate_results_hash`,
`ruleset_hash`, `required_context_ids`, `review_receipt_id`) as `verified`,
`skipped`, or `unverified` by presence on the receipt. Exit code is `0` for
consistent coverage, `1` for missing receipt or bad input, `2` for a malformed
admission shape (none of the four fields populated), and `3` when a required
field is absent; pass `--json` to emit the report alongside the exit code. The
verifier does not re-hash the receipt: `MergeAdmissionReceipt` is sealed and
`gate_results_hash` cannot be reproduced without `blast_radius`, which is not
a receipt field (#5400).
