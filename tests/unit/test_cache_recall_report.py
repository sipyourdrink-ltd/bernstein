"""Check the persisted operator recall report."""

import json
from pathlib import Path

from bernstein.core.persistence.cache_eviction import RecallSet, recall_report_path, write_recall_report


def test_recall_report_replaces_previous_result(tmp_path: Path) -> None:
    path = recall_report_path(tmp_path, "task-cache")
    write_recall_report(path, RecallSet("task-cache", "superseded", consumers=["old-run"]))
    write_recall_report(
        path,
        RecallSet(
            "task-cache",
            "operator eviction",
            tombstoned=["task-cache", "derived-cache"],
            consumers=["run-1", "run-2"],
        ),
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "root_key": "task-cache",
        "reason": "operator eviction",
        "tombstoned": ["task-cache", "derived-cache"],
        "consumers": ["run-1", "run-2"],
    }
