## Evidence pack JSON canonicalisation follows RFC 8785 (Issue #5504)

bernstein.compliance.evidence_pack now routes JSON artifact serialization and digest computation (manifest.json, controls.json, audit-chain/data_catalog.json) through canonical_envelope_bytes from bernstein.core.security.evidence_envelope. All emitted JSON artifacts adhere to RFC 8785 (JCS) canonical JSON, including UTF-16 code-unit property name sorting (§3.2.3).

**Existing packs note**: Previously exported evidence packs serialized JSON artifacts using local json.dumps(sort_keys=True, indent=2) formatting with a trailing newline. Because manifest.json records SHA-256 digests over the files as packaged, older packs remain self-consistent and verify against their own manifests. Newly generated packs produce RFC 8785 JCS canonical bytes for all embedded JSON artifacts. Holders of older packs wishing to align with the canonical JCS envelope standard can re-export their packs using bernstein audit export.
