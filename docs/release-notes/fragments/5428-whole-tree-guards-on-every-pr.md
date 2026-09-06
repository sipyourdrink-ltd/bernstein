## Whole-tree guard tests run on every pull request

A whole-tree guard asserts an invariant by scanning `src/` — no module under
`core/` unreachable, exactly one receipt-verify protocol, no unallowlisted
result-field collision. The affected-set selector builds its map from import
edges, and a guard has no import edge to the file a pull request adds. So a
change that adds the very thing a guard forbids ran green on its own checks and
failed first in the merge group, where it costs an ejection and takes every
entry queued behind it with it.

Guards now declare themselves with `@pytest.mark.whole_tree_guard`, and
`scripts/run_tests.py --affected` selects every marked file on every run
regardless of the diff. They are sharded like any other selected file, so the
added wall time is spread across the lanes.

`tests/unit/test_whole_tree_guards_are_marked.py` keeps the set declared rather
than inferred: it walks the test tree for modules that build a path from the
source root and then walk it, and fails when one carries no marker. The scan
that found the original two found eighteen more, all of which now run on the
pull request that would break them (#5428).
