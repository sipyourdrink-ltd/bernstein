## `bernstein identity export-verifier` (Issue #5115)

Adds `bernstein identity export-verifier`, which writes the install-identity JWKS
to a per-platform verifier file so a verifier can pin the trust anchor locally
instead of fetching `/.well-known/http-message-signatures-directory` at runtime.
`--target local` (default, operator workstation) writes
`~/.config/bernstein/verifier/local.json`; `--target server` writes
`~/.config/bernstein/verifier/server.json` (shared server filesystem). The command
writes the JWKS as canonical JSON and a `.json.sha256` sidecar, and skips the
write when the key content is unchanged since the last run — re-running after a
key rotation writes, re-running without rotation is a no-op. `--dry-run` prints
the destination path without writing anything.