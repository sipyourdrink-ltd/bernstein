"""Tests for cross-run project memory."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.retrospective import (
    append_to_project_memory,
)
from bernstein.core.retrospective import (
    gather_project_memory_from_json as gather_project_memory,
)
from bernstein.core.retrospective import (
    get_recent_project_memory_from_json as get_recent_project_memory,
)


@pytest.fixture
def temp_sdd_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd directory with memory subdirectory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    memory_dir = sdd / "memory"
    memory_dir.mkdir()
    return sdd


class TestAppendToProjectMemory:
    """Test appending run summaries to project memory."""

    def test_append_creates_file_if_missing(self, temp_sdd_dir: Path) -> None:
        """Appending to a non-existent file creates it."""
        memory_file = temp_sdd_dir / "memory" / "project_memory.json"
        assert not memory_file.exists()

        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-120000",
            goal="Test task",
            tasks_done=3,
            tasks_failed=0,
            cost_usd=0.15,
            lesson="All passed",
        )

        assert memory_file.exists()
        data = json.loads(memory_file.read_text())
        assert len(data) == 1
        assert data[0]["run_id"] == "20260329-120000"

    def test_append_adds_to_existing(self, temp_sdd_dir: Path) -> None:
        """Appending to an existing file adds a new entry."""
        memory_file = temp_sdd_dir / "memory" / "project_memory.json"

        # Append first entry
        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-120000",
            goal="First task",
            tasks_done=2,
            tasks_failed=1,
            cost_usd=0.10,
            lesson="Lesson 1",
        )

        # Append second entry
        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-121000",
            goal="Second task",
            tasks_done=3,
            tasks_failed=0,
            cost_usd=0.20,
            lesson="Lesson 2",
        )

        data = json.loads(memory_file.read_text())
        assert len(data) == 2
        assert data[0]["run_id"] == "20260329-120000"
        assert data[1]["run_id"] == "20260329-121000"

    def test_append_respects_max_20_entries(self, temp_sdd_dir: Path) -> None:
        """Project memory is limited to last 20 entries."""
        memory_file = temp_sdd_dir / "memory" / "project_memory.json"

        # Append 25 entries
        for i in range(25):
            append_to_project_memory(
                sdd_dir=temp_sdd_dir,
                run_id=f"20260329-{i:06d}",
                goal=f"Task {i}",
                tasks_done=i % 3,
                tasks_failed=(i + 1) % 3,
                cost_usd=0.10 * (i + 1),
                lesson=f"Lesson {i}",
            )

        data = json.loads(memory_file.read_text())
        assert len(data) == 20
        # Most recent entries should be preserved
        assert data[-1]["run_id"] == "20260329-000024"
        assert data[0]["run_id"] == "20260329-000005"

    def test_append_includes_timestamp(self, temp_sdd_dir: Path) -> None:
        """Appended entries include a timestamp."""
        memory_file = temp_sdd_dir / "memory" / "project_memory.json"
        before = time.time()

        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-120000",
            goal="Test task",
            tasks_done=1,
            tasks_failed=0,
            cost_usd=0.05,
            lesson="OK",
        )

        after = time.time()
        data = json.loads(memory_file.read_text())
        entry = data[0]
        assert "timestamp" in entry
        assert before <= entry["timestamp"] <= after


class TestGetRecentProjectMemory:
    """Test retrieving recent project memory entries."""

    def test_get_recent_returns_empty_if_file_missing(self, temp_sdd_dir: Path) -> None:
        """Getting memory when file doesn't exist returns empty list."""
        result = get_recent_project_memory(sdd_dir=temp_sdd_dir, limit=5)
        assert result == []

    def test_get_recent_returns_all_if_less_than_limit(self, temp_sdd_dir: Path) -> None:
        """Getting memory with fewer entries than limit returns all."""
        # Append 3 entries
        for i in range(3):
            append_to_project_memory(
                sdd_dir=temp_sdd_dir,
                run_id=f"20260329-{i:06d}",
                goal=f"Task {i}",
                tasks_done=1,
                tasks_failed=0,
                cost_usd=0.05,
                lesson="OK",
            )

        result = get_recent_project_memory(sdd_dir=temp_sdd_dir, limit=5)
        assert len(result) == 3

    def test_get_recent_respects_limit(self, temp_sdd_dir: Path) -> None:
        """Getting memory respects the limit parameter."""
        # Append 10 entries
        for i in range(10):
            append_to_project_memory(
                sdd_dir=temp_sdd_dir,
                run_id=f"20260329-{i:06d}",
                goal=f"Task {i}",
                tasks_done=1,
                tasks_failed=0,
                cost_usd=0.05,
                lesson="OK",
            )

        result = get_recent_project_memory(sdd_dir=temp_sdd_dir, limit=5)
        assert len(result) == 5
        # Most recent should come last (index -1)
        assert result[-1]["run_id"] == "20260329-000009"

    def test_get_recent_returns_in_chronological_order(self, temp_sdd_dir: Path) -> None:
        """Retrieved entries are in chronological order (oldest first)."""
        for i in range(3):
            append_to_project_memory(
                sdd_dir=temp_sdd_dir,
                run_id=f"run_{i}",
                goal=f"Task {i}",
                tasks_done=1,
                tasks_failed=0,
                cost_usd=0.05,
                lesson="OK",
            )

        result = get_recent_project_memory(sdd_dir=temp_sdd_dir, limit=5)
        assert [e["run_id"] for e in result] == ["run_0", "run_1", "run_2"]


class TestGatherProjectMemory:
    """Test formatting project memory for context injection."""

    def test_gather_returns_empty_string_if_no_memory(self, temp_sdd_dir: Path) -> None:
        """Gathering memory when file missing returns empty string."""
        result = gather_project_memory(sdd_dir=temp_sdd_dir)
        assert result == ""

    def test_gather_formats_memory_for_injection(self, temp_sdd_dir: Path) -> None:
        """Gathered memory is formatted for injection into planning context."""
        # Append some entries
        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-001",
            goal="Add auth",
            tasks_done=4,
            tasks_failed=1,
            cost_usd=0.42,
            lesson="JWT tests need separate test database",
        )
        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-002",
            goal="Improve coverage",
            tasks_done=3,
            tasks_failed=0,
            cost_usd=0.18,
            lesson="",
        )

        result = gather_project_memory(sdd_dir=temp_sdd_dir)

        # Should contain a header and entries
        assert "Recent run history" in result
        assert "Add auth" in result
        assert "Improve coverage" in result
        assert "JWT tests need separate test database" in result
        # Format should show task counts
        assert "4/5" in result or "done" in result

    def test_gather_includes_only_recent_entries(self, temp_sdd_dir: Path) -> None:
        """Gathered memory includes only last 5 entries."""
        # Append 10 entries
        for i in range(10):
            append_to_project_memory(
                sdd_dir=temp_sdd_dir,
                run_id=f"run_{i}",
                goal=f"Task {i}",
                tasks_done=i % 3,
                tasks_failed=(i + 1) % 2,
                cost_usd=0.05 * (i + 1),
                lesson=f"Lesson {i}",
            )

        result = gather_project_memory(sdd_dir=temp_sdd_dir)

        # Should include recent ones
        assert "Task 9" in result
        assert "Task 8" in result
        # Should NOT include older ones
        assert "Task 0" not in result


class TestProjectMemoryInContext:
    """Test that project memory is included in planning context."""

    def test_gather_project_memory_includes_entries(self, temp_sdd_dir: Path) -> None:
        """gather_project_memory_from_json formats memory for context injection."""
        # Append a memory entry
        append_to_project_memory(
            sdd_dir=temp_sdd_dir,
            run_id="20260329-test",
            goal="Test goal",
            tasks_done=2,
            tasks_failed=1,
            cost_usd=0.25,
            lesson="Test lesson",
        )

        result = gather_project_memory(sdd_dir=temp_sdd_dir)

        # Should include memory section
        assert "Recent run history" in result
        assert "Test goal" in result
