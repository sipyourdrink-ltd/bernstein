# Tool-call effect fixtures

Recorded connector returns used by `tests/unit/security/test_native_toolcall_evidence.py`
for #6270 slice 1.

`effect_record.json` pins:

- a JSON-RPC `tools/call` response whose `{error, result}` mapping hashes with
  the same `json.dumps(..., sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)` as `ToolCallIntent` argument digests;
- a unified diff whose UTF-8 SHA-256 matches `ResultBundle.patch_sha256`.

Timestamps and echoed request ids stay in the preimage. Slice 2 will recompute
these digests in the verifier; this fixture is the writer-side contract.
