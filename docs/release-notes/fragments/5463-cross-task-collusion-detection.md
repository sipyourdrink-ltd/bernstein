## Cross-task collusion detector module (#5463)

Adds CrossTaskCollusionDetector, a module that checks for lineage
dependencies between task pairs and runs invariant checks over their
combined outputs. Ships 5 colluding and 5 benign fixture pairs.

- Permissive-test, dangerous-shell-split, config-permission-widen,
  gate-disable, and sensitive-file-access-split invariants.
- Each flag names the invariant, pair ID, and both task IDs.
- Results are deterministic.
- This PR implements the detector module only; integration into
  gate_pipeline and merge receipts will follow.