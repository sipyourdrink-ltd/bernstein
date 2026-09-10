## An agent card's `kid` resolves to the key that signed it

Cards were signed under a fixed per-tenant `kid` (`agent-bernstein-orchestrator`) while archived keys were published in the JWKS under timestamped ones. A fixed string names the *tenant*, not the key, so after a rotation it resolved to the new key while cards signed minutes earlier still carried it - and a verifier that routes by `kid`, which `/.well-known/agent.json/keys` documents as supported, fetched the wrong key and failed. Only a verifier that tried every published key was rescued by the grace window, and that is the fallback rather than the contract.

Cards are now signed under the RFC 7638 thumbprint of the signing key, the shape `identity/http_signing.py` already used, and archived keys are published under their own thumbprints. The `kid` therefore changes exactly when the key changes, so an in-flight card names the key that actually signed it and the archived entry resolves it for as long as the grace window holds it open.

The historical fixed `kid` stays advertised in the JWKS, mapping to the tenant's current key, so a verifier that cached `agent-bernstein-orchestrator` resolves exactly what it resolves today.

That is compatibility and it is also a retained defect, for a bounded population. A card signed *before* this deploy carries the fixed kid, and after the next rotation that kid still resolves to the new key - so those cards still fail to route, exactly as they did before. The window is bounded by the card's own lifetime and by `max-age=3600` on the agent-card route, and it closes on its own as pre-deploy cards expire (#5109).
