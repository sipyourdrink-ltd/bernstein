# Hardware key evidence implementation plan

## Numbered property test matrix
1. Only configured DER root and F9 pins authorize a root/intermediate/F9/leaf chain; every link, issuer, CA constraint and explicit timestamp is checked. Substitution, expiration and bad signatures fail.
2. Yubico firmware, serial, PIN/touch policy and form factor decode with exact lengths; malformed/missing values fail. Public verdicts omit serial. Approval rejects NEVER.
3. Leaf SPKI equals the actual receipt signer; Ed25519 requires firmware >=5.7, P-256 is supported through the existing protocol.
4. Assertion authenticator data binds configured RP, UP and optional UV; type, challenge, origin, credential identifier and Ed25519/ES256 signatures bind the canonical approved payload.
5. Retained packed registration cryptographically binds credential ID and COSE key to pinned attestation certificates. Nonincreasing counters (including zero) warn without rejection.
6. Invalid encodings, algorithms, lengths and untrusted bytes return deterministic errors; repeated verdict JSON is byte-identical.
7. Bundled genuine vendor roots match a DER SHA256 manifest. Private fixture verification is local-only and serial-free.
8. Existing receipt kinds, canonical bytes and signature checks retain their contracts.

## Implementation sequence
- Add synthetic failing tests, narrow contextual receipt validation and P-256 signature support on the existing signing path.
- Implement offline PIV chain/extensions and FIDO2 CBOR/COSE/packed-registration verifiers with explicit immutable verification policy.
- Bundle vendor roots and provenance manifest; document custody limits and operator workflow.
- Run focused tests, protocol regressions, lint/type checks; verify private fixtures without emitting serial; commit.
