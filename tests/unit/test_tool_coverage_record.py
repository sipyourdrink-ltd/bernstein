"""Unit tests for tool coverage record computation and emission (issue #3769)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from bernstein.adapters.openai_agents_builtins import list_dir_in_workdir
from bernstein.core.tools.coverage import (
    ToolCoverageRecord,
    compute_corpus_digest,
)


def test_compute_corpus_digest_deterministic() -> None:
    paths = ["src/b.py", "src/a.py", "tests/test_a.py"]
    d1 = compute_corpus_digest(paths)
    d2 = compute_corpus_digest(list(reversed(paths)))
    assert d1 == d2
    assert d1.startswith("sha256:")
    expected = "sha256:" + hashlib.sha256(b"src/a.py\nsrc/b.py\ntests/test_a.py").hexdigest()
    assert d1 == expected


def test_compute_corpus_digest_empty() -> None:
    d = compute_corpus_digest([])
    assert d.startswith("sha256:")
    assert d == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_tool_coverage_record_round_trip() -> None:
    cov = ToolCoverageRecord(
        file_count=3,
        corpus_digest="sha256:abcd",
        coverage="complete",
        truncated=False,
        truncation_reason=None,
        exit_status=0,
        exit_checked=True,
    )
    d = cov.to_dict()
    restored = ToolCoverageRecord.from_dict(d)
    assert restored == cov


def test_search_walk_finds_nothing_emits_coverage_record(tmp_path: Path) -> None:
    """A tool call that walks a fixed set of files and finds nothing emits a coverage record."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    empty_dir = workdir / "empty_dir"
    empty_dir.mkdir()

    events: list[dict[str, object]] = []

    def _emit(event: dict[str, object]) -> None:
        events.append(event)

    result = list_dir_in_workdir(workdir, "empty_dir", emit=_emit)
    assert result == ""
    tool_results = [e for e in events if e.get("type") == "tool_result"]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr["status"] == "ok"
    assert tr["count"] == 0
    cov = tr.get("coverage")
    assert isinstance(cov, dict)
    assert cov["file_count"] == 0
    assert cov["corpus_digest"] == compute_corpus_digest([])
    assert cov["coverage"] == "complete"
    assert cov["truncated"] is False
    assert cov["truncation_reason"] is None


def test_truncated_walk_emits_coverage_record_with_truncation_reason() -> None:
    """A fixture walk that is truncated partway emits a coverage record with truncation details."""
    record = ToolCoverageRecord(
        file_count=10,
        corpus_digest=compute_corpus_digest(["a.py", "b.py"]),
        coverage="partial",
        truncated=True,
        truncation_reason="timeout",
        exit_status="timeout",
        exit_checked=True,
    )
    assert record.truncated is True
    assert record.coverage == "partial"
    assert record.truncation_reason == "timeout"
    d = record.to_dict()
    assert d["truncated"] is True
    assert d["truncation_reason"] == "timeout"


def test_unexecuted_search_emits_unexecuted_coverage_record(tmp_path: Path) -> None:
    """A tool call that never ran its search at all (target directory didn't exist) says so."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    events: list[dict[str, object]] = []

    def _emit(event: dict[str, object]) -> None:
        events.append(event)

    result = list_dir_in_workdir(workdir, "nonexistent", emit=_emit)
    assert "error" in result.lower()
    tool_results = [e for e in events if e.get("type") == "tool_result"]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr["status"] == "error"
    cov = tr.get("coverage")
    assert isinstance(cov, dict)
    assert cov["file_count"] == 0
    assert cov["coverage"] == "partial"
    assert cov["truncated"] is True
    assert cov["truncation_reason"] is not None
