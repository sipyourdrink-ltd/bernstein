## Per-principal cost attribution and budget ceilings

Added `bernstein cost --by principal`, `--by grant`, and `--by authorizing_identity` rollup dimensions to attribute spend to the principal who incurred it and the grant that authorized the call. The honesty rule requires both principal and grant to be named for a row to count as attributed. New `PrincipalEnvelope` and `PrincipalBudgetError` exports support budget ceilings with enforcement before state moves. The cost ledger now tracks attribution metadata per entry, and operators can reconcile in-memory floats to exact micro-USD integers.

(#4985)
