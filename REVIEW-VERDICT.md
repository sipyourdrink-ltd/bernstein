FIXED: 1 of 1 blocking findings

F1 (JSON shape mismatch between docs and code):
  Fixed by changing the code to emit canonical schema field names
  (gate_results_hash, ruleset_hash, required_context_ids, review_receipt_id)
  instead of the abbreviated names (gate_results, ruleset, context_ids,
  review_receipt) it was emitting. The docs example in
  docs/operations/verify-coverage.md lines 79-101 already used the canonical
  names — the code was the side that drifted. Updated the test assertion
  in tests/unit/test_verify_coverage_cmd.py that grepped for the old
  abbreviated name. 22/22 tests pass (6 coverage + 16 feature-matrix);
  ruff clean.
