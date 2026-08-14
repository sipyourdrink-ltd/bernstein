# Lineage: artifact provenance

Per-artifact provenance in two layers: Lineage v1 (Sigstore-style
transparency log: RFC 8785 JCS canonicalisation, sha256 entry hashes,
Ed25519 JWS) and `LineageSpine`, the single always-on Merkle+HMAC store
that every adapter artifact write routes through.

## Key files

| File | Purpose |
|---|---|
| `spine.py` | The always-on spine: one append-only chained JSONL store per run |
| `entry.py` | `LineageEntry` frozen dataclass; `canonicalise` / `entry_hash` |
| `identity.py` | `AgentCard` (A2A subset); Ed25519 `sign_detached` / `verify_detached` |
| `signed_write.py` | `seal_write` / `SignedLineageLog` signed-write path |
| `gate.py` | Lineage CI gate (ADR-009 §6.2) |

## Invariants

- Adapter artifact writes route through `LineageSpine.record` at the
  single write boundary in `../../adapters/base.py` - no per-adapter
  opt-in, no second write path (`spine.py` docstring, issue #2292).
- Spine entries chain: `entry_hash = H(prev_hash, artifact_path,
  content_hash, actor, step_id, model, timestamp)`. Changing the entry
  shape breaks head-hash verification for existing runs.
- The spine's HMAC tag reuses the audit-chain key
  (`../security/audit.py`); key handling rules from that module apply.
- A new `LineageEntry` field must be optional, default `None`, dropped
  from `_canonical_body` when `None`, and read back in
  `_entry_from_dict` (cf. `attachment_digests`) - that is what keeps
  every historical entry's bytes, HMAC and JWS valid.
- `parent_hashes` is the artefact's ancestry only: tip projection reads
  two or more as a *fork merge*, so other inputs need their own field.
- Design rationale: `docs/decisions/009-lineage-v1.md`.

## Testing

Single files only, e.g.
`uv run pytest tests/unit/test_lineage_record.py -x -q`; the
`test_lineage_*.py` files cover entries, stores, signing, and gates.
