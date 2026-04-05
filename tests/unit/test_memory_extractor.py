"""Tests for memory extraction from agent logs."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.memory_extractor import AgentMemory, MemoryExtractor


# ---------------------------------------------------------------------------
# Fixtures: sample log content
# ---------------------------------------------------------------------------

SAMPLE_LOG = """\
[2026-04-04 10:00:01] Starting task: implement memory extractor
[2026-04-04 10:00:05] Reading src/bernstein/core/models.py
[2026-04-04 10:01:00] error: ModuleNotFoundError: No module named 'bernstein.core.memory_extractor'
[2026-04-04 10:01:15] I decided to create the module from scratch because it didn't exist yet.
[2026-04-04 10:02:00] Created: src/bernstein/core/memory_extractor.py
[2026-04-04 10:02:30] error: IndentationError in memory_extractor.py line 42
[2026-04-04 10:02:45] Fixed the indentation issue by aligning the dataclass fields.
[2026-04-04 10:03:00] Modified: src/bernstein/core/task_lifecycle.py
[2026-04-04 10:03:30] I chose to use regex instead of LLM calls because extraction should be lightweight.
[2026-04-04 10:04:00] Created: tests/unit/test_memory_extractor.py
[2026-04-04 10:05:00] uv run pytest tests/unit/test_memory_extractor.py -x -q
[2026-04-04 10:05:15] 4 passed in 0.8s
"""

EMPTY_LOG = ""


# ---------------------------------------------------------------------------
# Extraction tests
# ---------------------------------------------------------------------------


def test_extract_from_sample_log(tmp_path: Path) -> None:
    """Extraction picks up files, error/fix pairs, and decisions."""
    log_file = tmp_path / ".sdd" / "runtime" / "sess-001.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text(SAMPLE_LOG, encoding="utf-8")

    extractor = MemoryExtractor(tmp_path)
    memory = extractor.extract_from_log(log_file, "implement memory extractor", "backend")

    assert memory.session_id == "sess-001"
    assert memory.task_title == "implement memory extractor"
    assert memory.role == "backend"

    # Should find the created/modified files.
    assert "src/bernstein/core/memory_extractor.py" in memory.files_modified
    assert "src/bernstein/core/task_lifecycle.py" in memory.files_modified
    assert "tests/unit/test_memory_extractor.py" in memory.files_modified

    # Should find at least one error-fix pair.
    assert len(memory.learnings) >= 1
    assert any("Fix" in l for l in memory.learnings)

    # Should find decision lines.
    assert len(memory.patterns_discovered) >= 1
    assert any("decided" in p or "chose" in p for p in memory.patterns_discovered)


def test_empty_log_produces_empty_memory(tmp_path: Path) -> None:
    """An empty log should yield an AgentMemory with no learnings."""
    log_file = tmp_path / "empty.log"
    log_file.write_text(EMPTY_LOG, encoding="utf-8")

    extractor = MemoryExtractor(tmp_path)
    memory = extractor.extract_from_log(log_file, "noop task", "qa")

    assert memory.session_id == "empty"
    assert memory.learnings == []
    assert memory.files_modified == []
    assert memory.patterns_discovered == []


def test_missing_log_produces_empty_memory(tmp_path: Path) -> None:
    """A non-existent log path should not raise, just return empty memory."""
    missing = tmp_path / "no-such-file.log"

    extractor = MemoryExtractor(tmp_path)
    memory = extractor.extract_from_log(missing, "ghost task", "backend")

    assert memory.learnings == []
    assert memory.files_modified == []
    assert memory.patterns_discovered == []


# ---------------------------------------------------------------------------
# Save / query roundtrip
# ---------------------------------------------------------------------------


def test_save_and_query_roundtrip(tmp_path: Path) -> None:
    """Saved memories can be retrieved via query."""
    extractor = MemoryExtractor(tmp_path)

    m1 = AgentMemory(
        session_id="s1",
        task_title="fix router",
        role="backend",
        learnings=["Import error fixed by adding __init__.py"],
        files_modified=["src/bernstein/core/router.py"],
        patterns_discovered=["decided to use cascade pattern"],
        timestamp=1000.0,
    )
    m2 = AgentMemory(
        session_id="s2",
        task_title="add lint checks",
        role="qa",
        learnings=["ruff --fix resolved all style issues"],
        files_modified=["src/bernstein/core/quality_gates.py"],
        patterns_discovered=[],
        timestamp=2000.0,
    )

    extractor.save(m1)
    extractor.save(m2)

    # Query all — newest first.
    all_memories = extractor.query()
    assert len(all_memories) == 2
    assert all_memories[0].session_id == "s2"
    assert all_memories[1].session_id == "s1"


def test_query_filter_by_role(tmp_path: Path) -> None:
    """Role filter returns only matching memories."""
    extractor = MemoryExtractor(tmp_path)

    extractor.save(AgentMemory(
        session_id="s1",
        task_title="task A",
        role="backend",
        learnings=["learn A"],
        timestamp=1.0,
    ))
    extractor.save(AgentMemory(
        session_id="s2",
        task_title="task B",
        role="qa",
        learnings=["learn B"],
        timestamp=2.0,
    ))
    extractor.save(AgentMemory(
        session_id="s3",
        task_title="task C",
        role="backend",
        learnings=["learn C"],
        timestamp=3.0,
    ))

    backend_only = extractor.query(role="backend")
    assert len(backend_only) == 2
    assert all(m.role == "backend" for m in backend_only)
    # Newest first.
    assert backend_only[0].session_id == "s3"

    qa_only = extractor.query(role="qa")
    assert len(qa_only) == 1
    assert qa_only[0].session_id == "s2"


def test_query_filter_by_file_pattern(tmp_path: Path) -> None:
    """File pattern filter matches against modified files."""
    extractor = MemoryExtractor(tmp_path)

    extractor.save(AgentMemory(
        session_id="s1",
        task_title="task A",
        role="backend",
        files_modified=["src/bernstein/core/router.py", "src/bernstein/core/models.py"],
        timestamp=1.0,
    ))
    extractor.save(AgentMemory(
        session_id="s2",
        task_title="task B",
        role="qa",
        files_modified=["tests/unit/test_router.py"],
        timestamp=2.0,
    ))

    router_memories = extractor.query(file_pattern="router")
    assert len(router_memories) == 2

    models_memories = extractor.query(file_pattern="models.py")
    assert len(models_memories) == 1
    assert models_memories[0].session_id == "s1"


def test_query_empty_store(tmp_path: Path) -> None:
    """Querying with no saved memories returns an empty list."""
    extractor = MemoryExtractor(tmp_path)
    assert extractor.query() == []
    assert extractor.query(role="backend") == []


def test_save_creates_directory(tmp_path: Path) -> None:
    """Save creates .sdd/memory/ if it does not exist."""
    extractor = MemoryExtractor(tmp_path)
    memory = AgentMemory(
        session_id="s1",
        task_title="test",
        role="backend",
        learnings=["learned something"],
        timestamp=1.0,
    )
    extractor.save(memory)

    learnings_path = tmp_path / ".sdd" / "memory" / "learnings.jsonl"
    assert learnings_path.exists()
    lines = learnings_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "s1"
    assert record["learnings"] == ["learned something"]
