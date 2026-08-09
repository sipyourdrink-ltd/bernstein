# Security: audit chain, identity, policy

The HMAC-chained audit log, Ed25519 install identity, and approval /
policy enforcement. The audit chain is the tamper-evident record other
subsystems anchor receipts to; treat its write path as load-bearing.

## Key files

| File | Purpose |
|---|---|
| `audit.py` | Immutable HMAC-chained audit log; daily JSONL rotation; chain crosses file boundaries |
| `audit_chain.py` | `AuditChainStore` facade plus the `EVENT_*` type constants |
| `audit_receipt.py` | Offline-verifiable receipt projection (COSE / in-toto) over a chain range |
| `agent_card_keystore.py` | Ed25519 install-identity keystore |
| `intent_capsule.py` | Signed task-goal capsules with drift escalation |
| `sigstore_attestation.py` | Rekor attestation with a local Ed25519 fallback; verifies local bundles |

## Invariants

- The HMAC key lives OUTSIDE the audit log directory and must be mode
  `0600`; a group- or world-readable key is a hard error at load time
  (`audit.py` module docstring).
- Event-type constants are append-only: add new `EVENT_*` names, never
  edit or reuse existing ones (`audit_chain.py` module docstring).
- Chain helpers accept the chain instance as a parameter (no singleton
  imports) and log through `log_with_prev_digest` so
  `prev_chain_digest` lands in the payload before the HMAC is computed
  (`audit_chain.py`).
- The audit chain is opt-in at runtime (`BERNSTEIN_AUDIT=1`, read in
  `../orchestration/orchestrator.py`); features must degrade cleanly
  without it.
- An attestation bundle is untrusted input. Its `public_key_file` names
  the key the signature is checked against, so it must stay a single
  plain filename inside `attestation_dir`, decided from the string with
  no `resolve` or `stat`, and read through a descriptor anchored to that
  directory. Never reintroduce a path-comparison containment check: it
  validates one lookup and the open performs another
  (`sigstore_attestation.py` module docstring, "Local bundle contract").

## Testing

Single files only, e.g. `uv run pytest tests/unit/test_audit.py -x -q`;
most surfaces here have a dedicated `test_audit_*.py` file.
