"""Exercise the linked-worktree snapshot example in a separate process."""

import json

from tests.unit.examples import run_demo


def test_snapshot_captures_unstaged_content_and_preserves_indexes(tmp_path):
    result = run_demo("snapshot-worktree", tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["main_index"] == "main staged"
    assert report["linked_index"] == "linked staged"
    assert report["snapshot_content"] == "linked unstaged"
    assert report["linked_worktree_removed"] is True
    assert report["metadata_after_removal"] == {"task_id": "demo-task", "label": "before edit"}
    assert list(tmp_path.iterdir()) == []
