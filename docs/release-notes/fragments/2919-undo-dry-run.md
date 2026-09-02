## ``bernstein undo --dry-run`` prints a task's change set without touching the tree

Added ``bernstein undo --dry-run`` to the ``undo`` command — running the flag on a worktree task prints the files that would be reverted and exits without modifying the working tree or any git state, letting operators audit a reversal before committing to it. (#2919)