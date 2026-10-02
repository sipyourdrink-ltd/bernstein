## Trust Record cnf.jwk.kid derived from RFC 7638 thumbprint

`cnf.jwk.kid` in exported Trust Records is now derived directly from the Ed25519 public key as an RFC 7638 JWK thumbprint (SHA-256 over canonical `{"crv":"Ed25519","kty":"OKP","x":"<x>"}`, base64url encoded without padding). Previously, default installations exported a constant `kid` (`install-0000000000000000`) derived from the default-disabled install revision token rather than from the signing key itself (#6087).
