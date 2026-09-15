## `bernstein bench` bundles are now actually signature-checked

`AgentCardSigner.sign` imported `bernstein.core.identity.agent_card_signer`,
a module that does not exist, so every production `bernstein bench run`
(without `--stub-signer`) silently caught the `ImportError` and signed with
`StubSigner`'s public test key instead. `BenchVerifier.verify` never
checked `signature`/`signer_fingerprint` at all, so a bundle's "signed"
claim was decorative either way.

`AgentCardSigner` now signs a detached Ed25519 JWS off the install identity
(mirroring `reliability.InstallIdentityReliabilitySigner`), and `bench
verify` rejects a bundle whose signature does not verify, with a new
`--signer-key` option to pass trusted install-identity public keys
(mirroring `bench reliability-verify`).

`bench verify` also prints which signer produced the bundle (`signer :
<fingerprint> (stub - test-grade)` or `(install identity)`), and a
`--require-install-identity` flag refuses a bundle that only carries a
valid stub signature. `AgentCardSigner` no longer mints a fresh install
identity as a side effect of signing: with no keypair on disk it raises,
pointing at `bernstein init`, instead of silently generating one nobody
has published.
