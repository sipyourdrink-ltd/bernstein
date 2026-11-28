# Snapshot a linked Git worktree

Run the example from the repository root with Git on your path.

```sh
uv run python examples/snapshot-worktree/demo.py
```

The example creates a temporary Git repository with a local test identity and
commit signing disabled. It stages different versions of `note.txt` in the
main and linked worktrees, then edits the linked copy once more.
`SnapshotStore.take()` captures that last edit. The JSON fields `main_index`
and `linked_index` show the two staged versions after capture.

After removing the temporary linked worktree, the example uses
`SnapshotStore.get()` in the main worktree to read the saved task ID and
label. It reads `snapshot_content` from the saved Git tree. The temporary
repository is removed on exit.

```sh
uv run pytest -q tests/unit/examples/test_snapshot_worktree_demo.py
```
