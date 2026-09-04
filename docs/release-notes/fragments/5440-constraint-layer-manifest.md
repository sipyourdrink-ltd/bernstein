## Constraint layer declared as unified manifest with immutable hash-locks

The evolution constraint layer is now defined in a single manifest covering the audit chain, identity/signing, policy/RBAC, and evolution admission/governance/invariants/gate/circuit modules. Any evolution proposal attempting to modify a module in the constraint manifest is refused at every level (L0-L3) with an audit entry. Hash-locking and CODEOWNERS drift guards verify full coverage across the constraint layer (#5440).
