## Witness co-signing of audit checkpoints

Adds an optional second party that co-signs audit checkpoints, so a rewind of
the chain segments and the checkpoints file together no longer verifies clean.
A witness holds per-origin monotonic state and an Ed25519 key; it co-signs only
when the submitted tree is a consistent extension of the last one it accepted,
and refuses with a named cause (`size_regression`, `state_mismatch`,
`inconsistent_extension`) otherwise. New CLI commands:

- `bernstein audit witness export` – write the newest checkpoint payload for a witness to check
- `bernstein audit witness cosign` – check a submitted checkpoint against witness state and co-sign it
- `bernstein audit witness record` – store a co-signature, checked under the witness public key you pin

`bernstein audit verify --witness-key <pub>` authenticates recorded
co-signatures and exits non-zero when the local history contradicts one;
`--witness-state <dir>` additionally reads the witness's own pins, which survive
a rollback that deleted the local co-signature file. Co-signatures are Ed25519,
so a third party can verify one offline with the public key alone.

(#3161)
