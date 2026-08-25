## Audit MACs are domain-separated per store

Every HMAC-chained store signed with the same raw key, so bytes minted for one
store could in principle verify in another. New audit entries carry scheme v2:
the MAC key is HKDF-SHA256-derived per store and the preimage is domain-tagged.
One place resolves what a scheme authenticates with, so the log verifier, the
local record check and the runtime startup guard reach the same verdict; v1
chains keep verifying unchanged, and an unknown scheme is a hard failure on
every path rather than a silent fall back (#4212).
