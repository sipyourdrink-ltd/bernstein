# Legacy canonical JSON bytes

`bernstein.core.canonicalization.canonical_bytes` supplies the existing signing
bytes for evidence bundles, security governance decisions and review receipts.
Their existing module-level names remain importable aliases, including
`core.evidence.bundle.canonical_bytes` and its `_canonical_bytes` alias.

The format is exactly Python `json.dumps` with `ensure_ascii=False`,
`separators=(",", ":")` and `sort_keys=True`, encoded as UTF-8. This preserves
Unicode code-point key ordering, Python float spellings and legacy `NaN` /
`Infinity` output. Lone surrogates still fail UTF-8 encoding. Signed payload
schemas, schema versions, hash preimages and signature preimages are unchanged.

This helper is distinct from the RFC 8785 implementation documented in
[JCS canonicalization](../security/jcs-canonicalization.md). Callers using other
formats remain unchanged. Converting a signed format requires an explicitly
versioned migration; this shared-helper slice does not complete #5094.
