"""Ordering of results across files in the local log search index."""

from pathlib import Path

from bernstein.core.observability.log_search import LogSearchIndex


def test_log_search_limit_uses_timestamp_order(tmp_path: Path) -> None:
    logs = tmp_path / ".sdd" / "logs"
    logs.mkdir(parents=True)
    older = "2026-01-02T10:00:00Z INFO hit older"
    newer = "2026-01-02T11:00:00Z INFO hit newer"
    (logs / "a.log").write_text(f"{older}\n", encoding="utf-8")
    (logs / "b.log").write_text(f"{newer}\n2026-01-02T12:00:00Z INFO unrelated\n", encoding="utf-8")

    index = LogSearchIndex(tmp_path)
    assert [entry.message for entry in index.search("hit", limit=1).entries] == [newer]
    assert [entry.message for entry in index.search("hit", limit=2).entries] == [newer, older]
