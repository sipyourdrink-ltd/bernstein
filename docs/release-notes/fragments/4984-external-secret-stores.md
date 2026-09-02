## Broker an operator's own secret store instead of copying secrets into ours

The secrets broker now treats an external secret store as a backend behind one contract: resolve a named secret, mint a short-lived credential, report a revocation. A spec names a secret by opaque reference (`<store>:<store-native path>`), the store keeps the value, and the grant chain records the grant, the identity of the issuing store, the audience and the expiry — never the value. Revoking the secret upstream is what stops Bernstein minting, so there is no second revocation list to keep in step. Stores register through a plugin hook (`provide_secret_store`), so no vendor SDK import lands in `bernstein.core`; a static check fails the build if one does. `SecretsBroker.bind_scoped()` binds a minted token into the environment for the duration of one step and removes it — or restores the operator's own value — on the way out.

(#4984)
