# Fix-Proof Quality Gate (`fix_proof`)

The `fix_proof` quality gate verifies that a bug-fix task's newly added or
modified tests actually reproduce the bug, pass once the fix is present, fail
again when non-test source edits are reverted, and introduce no regressions in
previously passing tests.

It activates for bug-fix tasks (`task_type: fix`, `kind: bug`, `fixes_issue`,
or issue-closing references such as `Fixes #123` in the task title/description)
when enabled project-wide, per role, or per plan. Newly generated
`bernstein.yaml` configs (`bernstein init` / `bernstein init-wizard`) enable
`quality_gates.fix_proof: true` by default.

## How the Four Arms Work

Before arm 1 runs, Bernstein freezes the proof test node IDs (`proof_node_ids`)
from the task diff's test paths and captures the baseline regression selection
(`regression_node_ids`) from the base tree.

| Arm | Tree checked out | Expected outcome | Failure verdict |
| --- | --- | --- | --- |
| 1 (`tests_on_base`) | Base tree + task test edits only | Assertion failure (`FAILED`) on a stable witness | `fail` ("the test does not reproduce the bug") |
| 2 (`full_change`) | Full task diff (tests + source fix) | All `proof_node_ids` pass (`PASSED`) | `fail` ("proof tests fail on the full change") |
| 3 (`source_reverted`) | Full diff with non-test source edits reverted | Same witness fails (`FAILED`) again | `fail` ("the test does not exercise the fix") |
| 4 (`regression_base` / `regression_full`) | Unmodified base vs. full diff | Every baseline-passing control test still passes | `fail` (lists broken `regression_ids`) |

All arms run sequentially inside throwaway detached git worktrees using the
same recorded Python interpreter, lockfile SHA-256, and pytest invocation
settings, repeated `fix_proof_repeats` times (default `2`). Worktrees are removed
on every exit path.

## `inconclusive` Refusals (Fail Closed)

A required `fix_proof` gate treats `inconclusive` identically to `fail` and
blocks completion whenever proof evidence is unreliable:

- **Error-only arm**: collection errors (such as importing a brand-new symbol
  added by the fix), fixture `setup`/`teardown` errors, or uncaught non-assertion
  errors produce `inconclusive`.
- **Unstable reruns**: any difference in selected test IDs or per-test outcomes
  across repeats marks the receipt `inconclusive`.
- **Quarantined proof tests**: if any test in `proof_node_ids` is quarantined by
  `FlakyDetector` (`.sdd/runtime/flaky_quarantine.json`), it cannot certify a
  bug fix and yields `inconclusive`.

## Configuration

Enable globally in `bernstein.yaml` (default in newly generated configs):

```yaml
quality_gates:
  fix_proof: true
  fix_proof_repeats: 2
  fix_proof_timeout_s: 120
```

Enable for specific roles only:

```yaml
quality_gates:
  fix_proof: false
  fix_proof_roles:
    - qa
    - ci-fixer
```

Or on a role via `role_model_policy`:

```yaml
role_model_policy:
  qa:
    fix_proof: true
```

Or per plan / per step in a YAML plan file:

```yaml
name: hotfix-412
fix_proof: true
stages:
  - name: fix
    steps:
      - title: "Fix #412: handle empty tax line"
        task_type: fix
```

## Worked Example

Suppose a repository has `src/calc.py` containing a bug (`total(items)` skips the
last item when `len(items) == 1`) and a baseline passing test in
`tests/test_existing.py::test_multi_item`. The agent submits:

- Test edit (`tests/test_bug_412.py::test_single_item`): asserts `total([5]) == 5`
- Source fix (`src/calc.py`): fixes the slice boundary

1. Bernstein freezes `proof_node_ids = ("tests/test_bug_412.py::test_single_item",)`
   before running arm 1 and freezes
   `regression_node_ids = ("tests/test_existing.py::test_multi_item",)` before
   arm 4.
2. **Arm 1 (`tests_on_base`)**: runs twice on `tree_arm1` (`base + tests/test_bug_412.py`).
   Both runs exit `1` with `assertion_failed` (`0 != 5`).
3. **Arm 2 (`full_change`)**: runs twice on `tree_arm2` (`base + tests + src/calc.py`).
   Both runs exit `0` (`passed`).
4. **Arm 3 (`source_reverted`)**: reverts `src/calc.py` back to base and runs twice on
   `tree_arm3`. Both runs exit `1` with `assertion_failed`.
5. **Arm 4 (`regression_base` / `regression_full`)**: `tests/test_existing.py::test_multi_item`
   passes on both `base_tree_id` and `tree_arm2`, leaving `regression_ids = ()`.
6. Bernstein seals the `FixProofReceipt` (git tree IDs, frozen node IDs,
   environment digest, per-arm exit statuses and durations, and verdict),
   anchors it onto `.sdd/lineage/fix-proof-<task_id>/spine.jsonl` via
   `LineageSpine`, and writes `.sdd/runtime/fix_proof/<task_id>.json`.

## Offline Verification

Reviewers can verify the sealed receipt offline — and re-run it directly from
its git tree IDs (unaffected by branch rebases or different commit SHAs):

```python
import json
from pathlib import Path
from bernstein.core.quality.fix_proof import (
    FixProofReceipt,
    rerun_fix_proof_on_trees,
    verify_fix_proof_receipt,
)

raw = json.loads(Path(".sdd/runtime/fix_proof/T-412.json").read_text())
receipt = FixProofReceipt.from_dict(raw)

ok, errors = verify_fix_proof_receipt(receipt, lineage_root=Path(".sdd/lineage"))
assert ok, errors

rerun = rerun_fix_proof_on_trees(Path("."), receipt)
assert rerun.verdict_fields() == receipt.verdict_fields()
```
