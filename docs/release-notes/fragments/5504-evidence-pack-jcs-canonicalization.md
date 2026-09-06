## Evidence packs now digest under RFC 8785 (JCS), not a local byte rule

Compliance evidence packs (`bernstein audit export`) digest `manifest.json`,
`controls.json` and `audit-chain/data_catalog.json` through the same RFC 8785
(JCS) canonical-JSON encoder the signed evidence-envelope format already
ships and pins with golden vectors
(`bernstein.core.security.evidence_envelope.canonical_envelope_bytes`),
instead of a hand-rolled `json.dumps(sort_keys=True, indent=2)` call local to
`compliance/evidence_pack.py`. A verifier who was handed the RFC 8785
specification, rather than this source tree, can now reproduce the same
bytes and the same digest.

**Effect on previously written packs.** JCS uses minimal separators (no
pretty-printing) and orders object keys by UTF-16 code unit rather than by
Unicode code point, so a pack built before this change and one built after
it from identical input do not share the same per-artefact SHA-256 or the
same top-level bundle `sha256` -- even though nothing about *what* is
recorded changed. A pack already delivered to a regulator or auditor still
verifies against the encoding rule that was in effect when it was produced
(`json.dumps(sort_keys=True, indent=2)`); it does not retroactively become
invalid, and nothing needs to be re-issued. Only newly generated packs use
the RFC 8785 encoding (#5504).
