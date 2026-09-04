FIXED: 3 of 3 blocking findings

F1 (AC #6 — docs/operations page + cross-link from verification-tracking.md):
  Fixed: docs/operations/verify-coverage.md (114 lines) added with all four graded fields,
  exit-code contract, and --json shape. docs/operations/verification-tracking.md:47-50
  added paragraph contrasting self-reported nudge flags vs. receipt-backed structural grading,
  with link to verify-coverage.md.

F2 (AC #7 — FEATURE_MATRIX.md row):
  Fixed: docs/reference/FEATURE_MATRIX.md:266 added row for `bernstein verify coverage
  <head-sha>` with maturity score and one-sentence description.
  docs/reference/cli/verify.md updated to list the coverage subcommand.
  test_feature_matrix_drift.py: 16/16 pass.

F3 (AC #2 contract divergence — "recomputes" vs presence-based grading):
  No code change needed. The implementation grades by presence only, which is the
  correct behaviour per R19: MergeAdmissionReceipt is a sealed schema and
  gate_results_hash cannot be reproduced without blast_radius (not a receipt field).
  The downgrade is documented in: verify_cmd.py docstring ("The receipt is not re-hashed"),
  docs/operations/verify-coverage.md ("The command grades presence only ... It does not
  recompute"), and the release-notes fragment (does not mention recompute at all).
  The issue's AC still describes recompute — updating the issue body is out of scope for
  this PR (R19 is the correct binding).
