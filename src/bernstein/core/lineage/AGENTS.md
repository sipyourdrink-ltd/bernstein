# Lineage: artifact provenance

Per-artifact provenance in two layers: Lineage v1 (Sigstore-style transparency log: RFC 8785 JCS canonicalisation, sha256 entry hashes, Ed25519 JWS) and `LineageSpine`, the single always-on Merkle+HMAC store every artifact write routes through.

## Key files

| File | Purpose |
|---|---|
| `spine.py` | The always-on spine: one append-only chained JSONL store per run |
| `entry.py` | `LineageEntry` frozen dataclass; `canonicalise` / `entry_hash` |
| `identity.py` | `AgentCard` (A2A subset); Ed25519 `sign_detached` / `verify_detached` |
| `signed_write.py` | `seal_write` / `SignedLineageLog` signed-write path |
| `coverage.py` | Anchors a `ToolCoverageRecord` (issue #3769) as a `"coverage"`-kind entry keyed by `tool_call_id` (issue #3770). Anchors a `content_hash` commitment only, not the record's bytes - a reader that cannot independently recover the payload must treat the claim as unverified, never fabricate a passing record from the entry alone |
| `activity.py` | Active-set closure and provenance graph resolution over the receipt ledger |
| `gate.py` | Lineage CI gate (ADR-009 §6.2) |
| `run_graph.py` | Pairs each fan-out worktree branch with the spine that recorded it: `build_run_graph` returns one `RunGraphNode` per branch (`session_id`, `head_sha`, `run_id`, `spine_head_hash`) plus a deterministic root hash. Pure - it adds no storage and resolves `session_id -> run_id` from a caller-supplied mapping |
| `merge_provenance.py` | Records one row per path a CLI agent's work lands through a merge (issue #2789). Reads `before..after` so a fast-forward records like a true merge, hashes the git blob rather than the working tree, and names the merge commit in `step_id` - so a third party recomputes every `content_hash` from the repository alone. Recording must never undo a landed merge: `spawner_merge` logs a failure and keeps the merge, and neither reading the base nor writing the rows may gate it |

## Invariants

- Adapter artifact writes route through `LineageSpine.record` at the single write boundary in `../../adapters/base.py` - no per-adapter opt-in, no second write path (issue #2292).
- A CLI adapter's subprocess writes files itself and never reaches that in-process boundary, so `merge_provenance.py` records them where the work enters the repository. It is a second *caller* of `record_artifact_write`, not a second write path.
- Spine entries chain: `entry_hash = H(prev_hash, artifact_path, content_hash, actor, step_id, model, timestamp)`. Changing the entry shape breaks head-hash verification for existing runs.
- The spine's HMAC tag uses a per-store key derived from the audit-chain master key with HKDF-SHA256 (`../security/key_derivation.py`): v2 MACs the derived key over a domain-tagged preimage, v1 keeps the raw key untagged. `../security/audit.py`'s key rules apply.
- A new `LineageEntry` field must be optional, default `None`, dropped from `_canonical_body` when `None`, and read back in `_entry_from_dict` (cf. `attachment_digests`) - that is what keeps every historical entry's bytes, HMAC and JWS valid.
- `parent_hashes` is the artefact's ancestry only: tip projection reads two or more as a *fork merge*, so other inputs need their own field.
- Design rationale: `docs/decisions/009-lineage-v1.md`.

## Testing

Single files only, e.g. `uv run pytest tests/unit/test_lineage_record.py -x -q`; the `test_lineage_*.py` files cover entries, stores, signing, and gates. Merge recording is tested against real git repositories, not a stub - what it asserts is that a row's `content_hash` is the blob git stored (`tests/unit/lineage/test_merge_provenance.py`).

<!-- Reviewed 2026-08-28 against this subtree; the notes above still hold. -->
