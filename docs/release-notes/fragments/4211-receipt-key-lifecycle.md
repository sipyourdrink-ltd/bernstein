## Receipt-signing keys gain a rotation and revocation lifecycle

`bernstein verify receipt` accepts `--key-chain`, a signed key-succession
chain that widens the provenance pin from one key to a key generation.
`--public-key` then pins the chain's root key and the receipt's own key is
resolved through the chain, so rotating a key no longer invalidates receipts
an auditor already holds: a key that was rotated out but never revoked
verifies with verdict `superseded`. A revoked key stops carrying trust and
exits `4`, with a verdict naming which side of the revocation instant the
signature falls on. Receipt bytes carry no wall clock, so that instant comes
from `--signed-at`; without it a revoked key fails closed. `--json` reports
the verdict as `key_verdict`. Verification with no chain is unchanged. (#4211)
