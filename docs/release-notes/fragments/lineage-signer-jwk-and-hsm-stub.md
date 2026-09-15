## The Phase-1 lineage signer advertises its key like every adapter does

`signer_from_config` returns either an `Ed25519FileKeySigner` (the `key_path=` route) or a `KMSAdapter` (the `kms_adapter=` route). Every adapter has `public_key_jwk()`; the file signer had only `public_key_bytes()`, so the same key on the same disk exposed a different surface depending on which config shape the operator wrote, and an auditor's JWK-based attestation flow worked or broke on that distinction alone. `Ed25519FileKeySigner.public_key_jwk()` now delegates to the encoder the adapters use - promoted from `_public_key_jwk` to `public_key_jwk_for` - so the two paths cannot drift into disagreeing about the encoding.

## `key_kind: hsm` says it is unimplemented rather than unrecognised

The Phase-1 dispatcher rejected `key_kind='hsm'` with the same generic "unsupported key_kind" error it gives a typo, leaving an operator unable to tell "not yet implemented" from "refused on purpose". It now names HSM specifically and points at the Phase-2 `kms_adapter='hsm'` route that dispatches to a real integration. `HSMSigner` is the named stub that route documents; it is deliberately *not* a subclass of `HSMKMSAdapter`, because customer integrations are discovered through `HSMKMSAdapter.__subclasses__()` and a subclass of the stub would be invisible to that lookup (#5099).
