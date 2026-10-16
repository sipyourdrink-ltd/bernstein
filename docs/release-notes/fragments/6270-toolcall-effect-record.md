## Tool-call effect digest recorded after connector return

When the attestation interlock is wired, the native tool-call evidence
provider now appends a `toolcall.effect` audit event after the connector
returns, bound to the same `intent_digest` as the attestation and dispatch
markers. It records `effect_digest` (SHA-256 of the connector's JSON-RPC
response line exactly as received, or the `ResultBundle.patch_sha256`
preimage for patches and string results; JSON `null` on timeout), `outcome`
(`ok` / `error` / `timeout` / `partial`) and `duration_ms`. A missing effect
record reads as unobserved, never success. `bernstein gateway start` does not
wire the interlock by default. (#6270)
