# Audit Receipts and Lineage

Lineage is the foundation of offline auditability in Bernstein.

## Receipt Specifications
1. **DSSE Envelope Format**: Receipts enforce Dead Simple Signing Envelope (DSSE) encapsulation and error validation in `src/bernstein/core/security/audit_dsse.py:100`.
2. **Receipt Invariants**: Run attestation and validation error models are defined in `src/bernstein/core/security/audit_receipt.py:118`.
3. **Offline Verification**: Standalone verification CLI verifies receipts in `verify_cli/bernstein_verify_receipt/verify.py:76` and entrypoint `src/bernstein/core/verifier/audit_receipt_verifier.py:28`.
