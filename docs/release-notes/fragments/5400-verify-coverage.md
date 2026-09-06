## `bernstein verify coverage` subcommand for merge admission receipts

Adds the `coverage` subcommand to the `bernstein verify` command group, enabling
operators to inspect and cryptographically recompute the structured coverage sets
(`verified`, `unverified`, and `skipped`) recorded on a merge admission receipt.

Running `bernstein verify coverage <head-sha>` loads the receipt from
`.sdd/merges/receipts/<hash>.json`, recomputes the `coverage_set_hash` from the
receipt's own path sets, and renders a structured breakdown of what was exercised,
what remained unverified, and which oracle scopes were skipped and why.
The command exits 0 when the coverage sets recompute byte-for-byte, 1 when no
receipt is found or when inspecting a legacy schema v1 receipt, and 2 on any
divergence or tamper detected in `coverage_set_hash`. Passing `--json` emits
a machine-readable JSON object for automated audit pipelines.

(#5400)
