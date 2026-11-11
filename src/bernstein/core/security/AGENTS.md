# Security: audit chain, identity, policy

The HMAC-chained audit log, Ed25519 install identity, and approval / policy enforcement. Other subsystems anchor receipts to the chain, so its write path is load-bearing.

## Key files

| File | Purpose |
|---|---|
| `audit.py` | Immutable HMAC-chained audit log; daily JSONL rotation; chain crosses file boundaries |
| `audit_chain.py` | `AuditChainStore` facade plus the `EVENT_*` type constants; the three `mcp.*` signing-gate events are shape-checked on write and a malformed one raises (#6913) |
| `audit_receipt.py` | Offline-verifiable receipt projection (COSE / in-toto) over a chain range |
| `agent_card_keystore.py` | Persistent Ed25519 keystore for A2A agent-card signing |
| `intent_capsule.py` | Signed task-goal capsules with drift escalation |
| `sigstore_attestation.py` | Rekor attestation with a local Ed25519 fallback; verifies local bundles |
| `result_receipt_bundle.py` | Result receipt bundle with offline DSSE verification |
| `capability_delta.py`, `surface_grant_delta.py` | Pure grant comparisons (`AgentPermissions`; workflow/Dockerfile/Compose); no production caller yet |
| `key_custody.py`, `key_derivation.py` | Ed25519 signing custody (file/env; HSM is a subclassable stub) and HKDF-SHA256 per-store key derivation with scheme versioning |
| `approval_decision.py` | Signed, nonce-bound decision records read by the task approval gates |
| `auth_rate_limiter.py` | Per-client request and auth-attempt limits keyed by the real TCP peer; exhausted windows and failed credentials start a bounded exponential lockout (#6874) |
| `external_policy_hook.py` | `PolicyHookRegistry` for OPA/Cedar/custom hooks; a registry passed to `AgentSpawner` gates each resolved dispatch and fails closed: anything but an explicit ALLOW stops the action, and a refusal never selects another executor (`../agents/spawner_core.py`, #6956) |
| `dlp_scanner.py` | `DLPScanner` behind the `dlp_scan` gate: secrets and regulated data in changed files, with value-scoped allowlisting |

## Invariants

- The default HMAC master key lives outside audit logs at mode `0600`; POSIX loaders reject group/world access, while mode enforcement is skipped on Windows (`audit.py`).
- Scheme v2 separates store keys with HKDF-SHA256 and prefixes domain tags into chain preimages. Legacy v1 records use the raw master key without that prefix and remain readable (`key_derivation.py`).
- An approval decision file counts only as a signed record bound to the task and the open request nonce; anything else in a decision slot rejects (`approval_decision.py`).
- Event-type constants are append-only: add `EVENT_*` names, never edit or reuse existing ones (`audit_chain.py`).
- Chain helpers take the chain as a parameter (no singletons) and log through `log_with_prev_digest`, so `prev_chain_digest` is in the payload before the HMAC.
- Runtime audit mode is enabled by `BERNSTEIN_AUDIT=1` or compliance `audit_logging` (`../orchestration/orchestrator.py`); features degrade without it.
- Untrusted paths are opened, never compared: a comparison validates one lookup while the open performs another (`sigstore_attestation.py`).
- Where supported, tenant writes and rotation walk the subtree through `TenantPaths.anchor` (`../persistence/anchored_write.py`). Descriptor walks require `dir_fd`, `O_NOFOLLOW` and `O_DIRECTORY`; platform fallbacks do not provide equivalent symlink containment (`ANCHORED_{WRITE,ROTATE}_SUPPORTED`).
- `AuditLog.verify_incremental()` trusts only signed or HMAC-chained material. The signed checkpoint pins sealed prefixes by hash and length; missing or truncated segments are reported unless covered by a checkpoint-bound `ack-tear` record. Unsigned hash tiles are checked against the bytes, never used to skip them (#6435, #6443), and are read before the segments they describe so a seal landing mid-run is not a fault (#6548).
- DLP allowlist entries mark a *value* as benign, not the line: a rule skips only a match that overlaps an allowlisted span. Only operator `allowlist_patterns` suppress a whole line (`dlp_scanner.py`, #6430).

## Testing

Single files only: `uv run pytest tests/unit/test_audit.py -x -q`.

<!-- Reviewed 2026-11-11 against main 535af7532; the notes above still hold. -->
