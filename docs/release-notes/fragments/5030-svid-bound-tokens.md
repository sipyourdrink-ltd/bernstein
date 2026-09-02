## Tokens can be bound to the X.509-SVID that must present them

An SSO token can now be issued bound to a workload's X.509-SVID, carrying an
RFC 8705 `x5t#S256` confirmation claim over the SVID leaf. The auth middleware
recomputes the thumbprint from the certificate the caller actually presented,
so a bound token replayed from anywhere else is refused rather than honoured.
Binding is opt-in per audience, and a token that carries a confirmation claim
is checked whatever the deployment configures. Every refusal appends an
`identity.token_binding_refusal` event to the audit chain naming which proof
failed, the SPIFFE ID that should have been presented, and the expected and
presented thumbprints — never the token itself. (#5030)
