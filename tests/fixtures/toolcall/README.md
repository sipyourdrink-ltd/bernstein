# Tool-call effect fixtures

Recorded connector returns used by `tests/unit/security/test_native_toolcall_evidence.py`
for #6270 slice 1.

`effect_record.json` pins:

- a JSON-RPC `tools/call` response line (`received`) hashed as the stripped
  bytes the connector sent -- its spacing differs from compact JSON, so a
  re-serialised digest would not match;
- a unified diff whose UTF-8 SHA-256 matches `ResultBundle.patch_sha256`.

A connector that echoes a timestamp produces a different digest. Slice 2 will
recompute these digests in the verifier; this fixture is the writer-side
contract.
