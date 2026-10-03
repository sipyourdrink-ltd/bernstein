## `bernstein bench verify` checks the bundle signature

A `SubmissionBundle` carried a `signature` and a `signer_fingerprint`, and nothing
verified either. `bench verify` checked the suite hash, every receipt hash, the
task hashes and the replayed verdicts — all of which a forger recomputes — and
never the one field that needs a key.

The production signer could not engage either: it imported a module that does not
exist, so its fallback ran on every call and every bundle produced without
`--stub-signer` was signed by the stub, under a key that is a public constant.

Now: the production signer uses the install identity (detached Ed25519 JWS, the
same shape run receipts use) and **fails** rather than falling back. `bench verify`
checks the signature by default. Supply the signer's public key with
`--trusted-key FINGERPRINT=PATH`; a bundle whose fingerprint resolves to no
trusted key is reported `UNSIGNED` rather than assumed good, and a stub-signed
bundle verifies only with `--stub-signer`.

`--no-signature` skips the check for a caller who has no keys and wants replay
only. That is a decision in the log; the omission was not.
