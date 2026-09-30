## The worktree symlink warning tests no longer read the worker's logging state

Four assertions in `test_worktree_symlinks.py` checked `caplog.records` for the
"Failed to symlink" warning without pinning the capture level, so they were
asserting on whatever level `bernstein.core.git.worktree` happened to carry on
that pytest-xdist worker. A neighbour that raises the level and does not restore
it empties the records while the code under test behaves correctly, which is how
these four failed together in 2 of 6 full-suite runs. Each now pins the level,
and a regression test sets the logger to ERROR first and shows the warning is
still seen (#5954).
