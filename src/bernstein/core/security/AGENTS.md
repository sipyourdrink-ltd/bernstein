# Security: audit chain, identity, policy

The HMAC-chained audit log, Ed25519 install identity, and approval / policy enforcement. Other subsystems anchor receipts to the chain, so its write path is load-bearing.

## Key files

| File | Purpose |
|---|---|
| `audit.py` | Immutable HMAC-chained audit log; daily JSONL rotation; chain crosses file boundaries |
| `audit_chain.py` | `AuditChainStore` facade plus the `EVENT_*` type constants |
| `audit_receipt.py` | Offline-verifiable receipt projection (COSE / in-toto) over a chain range |
| `agent_card_keystore.py` | Ed25519 install-identity keystore |
| `intent_capsule.py` | Signed task-goal capsules with drift escalation |
| `sigstore_attestation.py` | Rekor attestation with a local Ed25519 fallback; verifies local bundles |
| `result_receipt_bundle.py` | Result receipt bundle with offline DSSE verification |

## Invariants

- The HMAC key lives OUTSIDE the audit log directory and must be mode `0600`; a
  group- or world-readable key is a hard error at load time (`audit.py`).
- Event-type constants are append-only: add `EVENT_*` names, never edit or reuse
  existing ones (`audit_chain.py` module docstring).
- Chain helpers take the chain as a parameter (no singleton imports) and log
  through `log_with_prev_digest`, so `prev_chain_digest` lands in the payload
  before the HMAC (`audit_chain.py`).
- The audit chain is opt-in at runtime (`BERNSTEIN_AUDIT=1`, read in
  `../orchestration/orchestrator.py`); features degrade without it.
- Untrusted paths are opened, never compared: a path-comparison check validates
  one lookup while the open performs another (`sigstore_attestation.py`).
- Same rule for tenant writes: the whole subtree (`backlog`, `metrics`,
  `runtime`, `audit`, ...) is created and opened through `TenantPaths.anchor`
  (`../persistence/anchored_write.py`), rotation included. Needs `dir_fd` +
  `O_NOFOLLOW`; lacking either, the refusal narrows to the final component or
  vanishes, never weakens (`ANCHORED_{WRITE,ROTATE}_SUPPORTED`).

## Testing

Single files only: `uv run pytest tests/unit/test_audit.py -x -q`.

<!-- Reviewed 2026-08-18 against this subtree; the notes above still hold. -->
