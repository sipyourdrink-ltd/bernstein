## Run receipt binding bytes can be signed as RFC 8785 (JCS)

`build_run_receipt` accepts `hash_profile="jcs-v2"` to sign the subject
binding as RFC 8785 canonical JSON instead of the legacy `py-json-v1`
(`json.dumps(..., ensure_ascii=True)`) bytes, so a run whose journal or spine
carries non-ASCII text (a task title, a model name) hashes the same way here
as it already does in the TRACE projection and audit-chain digests. The
default stays `py-json-v1`; existing receipts and callers are unaffected.
`verify_run_receipt` reads the profile from the receipt (absent means
`py-json-v1`) and fails closed on an unrecognised value. Slice 1 of #5274;
the journal and lineage-spine hashes are unchanged pending later slices.
