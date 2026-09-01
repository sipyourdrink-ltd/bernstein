## `bernstein trace export` emits TRACE 0.2 Trust Records

A new `bernstein trace export <RUN_ID>` subcommand (plus `--last`, `--out`,
`--json`, and `--sdd-dir` flags) lets operators extract a finished run's
execution evidence as a signed TRACE 0.2 Trust Record from local state —
no OTLP endpoint, no collector, no network. The record proves the journal
chain is intact and binds the run to the install identity via an Ed25519
signature. The trace extra (`bernstein[trace]`) is required (#4667).
