<!-- 5086-idempotent-govern-apply.md -->
### Idempotent reconciliation apply and ChangeReceipt construction (#5086)

- Added `compute_idempotency_key` in `bernstein.core.govern.reconcile_apply` deriving keys from `(entity_id, sha256(desired_value), policy_set_hash)`.
- Re-running reconciliation against an unchanged environment writes `ChangeReceipt` entries with `outcome="skipped"` and executes zero side effects.
- Implemented partial apply resumption to only execute non-succeeded or unsatisfied diff entries.
