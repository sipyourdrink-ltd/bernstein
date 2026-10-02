## The worktree symlink warning tests no longer read the worker's logging state

Four assertions in `test_worktree_symlinks.py` checked `caplog.records` for the
"Failed to symlink" warning without pinning the capture level, so they were
asserting on whatever level `bernstein.core.git.worktree` happened to carry on
that pytest-xdist worker. A neighbour that raises the level and does not restore
it empties the records while the code under test behaves correctly, which is
enough to make these four fail together, as they did in 2 of 6 full-suite runs.
Each now pins the level, and a regression test sets the logger to ERROR first
and shows the warning is still seen (#5954).

The pin holds because `caplog.at_level(..., logger=...)` sets both the named
logger's level and the capture handler's, and lifts a `logging.disable`, for the
duration of the block (pytest 9.1.1). It does not restore propagation, so a
neighbour that set `propagate = False` on this logger would still hide the
records; nothing in `src/` does that today.
