## Canonical JSON numbers follow RFC 8785

`canonicalize_jcs` produces the bytes every signature and digest in Bernstein is taken over. Its number serialiser used Python's `repr`, which generates the shortest round-trip digits RFC 8785 section 3.2.2.3 asks for but lays them out differently in three places: an integer-valued float kept a trailing `.0` (`10.0` rather than `10`), the exponent was padded (`1e-07` rather than `1e-7`), and the switch into scientific notation happened at different magnitudes than the ECMAScript rule the specification defers to. Negative zero kept its sign.

Signed agent-card bodies carry `max_budget_usd`, `created_at` and `expires_at` as floats, so an independent RFC 8785 verifier recomputing the canonical bytes read a valid Bernstein signature as invalid whenever one of those values landed on an integer boundary. Numbers now follow the ECMAScript `Number::toString` rule, and the RFC 8785 reference vector `structures.json` passes.

`JCS_CANONICALIZATION_VERSION` moves from 2 to 3. Any artefact whose body carries such a float has to be re-signed, and verifying parties upgrade first. `docs/security/jcs-canonicalization.md` says which artefacts are affected and which are not.
