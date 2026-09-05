## `bernstein runs scorecard` subcommand

`bernstein runs` gains a `scorecard` subcommand that builds or verifies the
per-run scorecard artifact from a run's work ledger. Without `--verify`, the
command calls `build_run_scorecard` over the run's `WorkLedger` and writes the
content-addressed envelope to `<root>/.sdd/runs/<run_id>/scorecard/` via
`write_scorecard_artifact`; with `--verify`, it re-derives the scorecard and
compares it against the on-disk artifact through `verify_scorecard`, exiting
non-zero on a mismatch so a CI gate can refuse a drifted artifact. `--json`
emits a stable machine-readable payload in both modes (the envelope on build,
the `VerifyResult` on verify), and `--workdir` selects the project root
containing the ledger.

(#5404)
