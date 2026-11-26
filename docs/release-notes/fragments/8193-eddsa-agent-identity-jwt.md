## EdDSA (Ed25519) signing for agent identity JWTs

Agent identity tokens issued by `AgentIdentityStore.create_identity` are now
signed with asymmetric `EdDSA` (Ed25519) rather than symmetric `HS256`, using an
out-of-workdir signing key governed by `KMSAdapter` (`0600` mode enforced, keys
under `.sdd/` refused). The corresponding public key is published in
`/.well-known/agent.json/keys` under its own `kid` matching the token header so
separate processes and third parties can verify agent tokens offline with public
material alone. `_authenticate_jwt` selects its verifier strictly from the
MAC-authenticated credential record's stored `algorithm`, refusing header
mismatches and algorithm confusion. Existing unexpired `HS256` credentials
continue verifying until expiry; `BERNSTEIN_AUTH_JWT_SECRET` logs a deprecation
warning and will be removed one minor release later.
