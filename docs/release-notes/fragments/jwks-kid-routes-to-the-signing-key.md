## An agent card's `kid` resolves to the key that signed it

Cards were signed under a fixed per-tenant `kid` (`agent-bernstein-orchestrator`) while archived keys were published in the JWKS under timestamped ones. A fixed string names the *tenant*, not the key, so after a rotation it resolved to the new key while cards signed minutes earlier still carried it - and a verifier that routes by `kid`, which `/.well-known/agent.json/keys` documents as supported, fetched the wrong key and failed. Only a verifier that tried every published key was rescued by the grace window, and that is the fallback rather than the contract.

Cards are now signed under the RFC 7638 thumbprint of the signing key, the shape `identity/http_signing.py` already used, and archived keys are published under their own thumbprints. The `kid` therefore changes exactly when the key changes, so an in-flight card names the key that actually signed it and the archived entry resolves it for as long as the grace window holds it open.

The historical fixed `kid` stays advertised in the JWKS, mapping to the tenant's current key, so a verifier that cached `agent-bernstein-orchestrator` resolves exactly what it resolves today (#5109).
