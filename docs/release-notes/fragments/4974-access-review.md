## `bernstein identity review` derives a signed access review from the identity chains

A new `bernstein identity review` command group derives a per-principal access
review from the HMAC-verified delegation receipts and grant chains, signs the
result twice (sha256 content digest plus an Ed25519 signature by the install
manager identity), and lets a reviewer record a sign-off as its own HMAC-chained
event. Subcommands cover derivation (`review`), sign-off (`review sign-off`),
and digest verification (`review verify`); `--format csv` renders the same rows
for a spreadsheet. The projection reads no wall clock, so the same window over
the same chain state is byte-identical — reviewers can verify the document
without trusting the tool that produced it.

(#4974)
