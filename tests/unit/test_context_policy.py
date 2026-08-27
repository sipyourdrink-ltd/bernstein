"""Tests for context policy determinism and opt-in scoping.

This module tests that:
1. Context receipts are deterministic (same inputs → identical hashes)
2. Scoping works correctly (subtree AGENTS.md files are picked up)
3. Default policy behavior matches current (non-scoped) behavior
4. Receipt schema validates correctly with policy fields
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# -----------------------------------------------------------------------
# Test fixtures
# -----------------------------------------------------------------------


@pytest.fixture
def sample_role_content() -> str:
    """Sample role context content."""
    return "You are a backend engineer. Focus on Python and performance."


@pytest.fixture
def sample_tasks_content() -> str:
    """Sample tasks context content."""
    return "Task 1: Fix the bug.\nTask 2: Add tests.\nTask 3: Review PR."


@pytest.fixture
def sample_lessons_content() -> str:
    """Sample lessons context content."""
    return "Lessons learned from previous runs: always check edge cases."


@pytest.fixture
def sample_agents_md_content() -> str:
    """Sample AGENTS.md content for subtree scoping."""
    return """# src/bernstein/adapters

Adapter modules for CLI agent integration.
Key files: claude.py, openai_agents.py, q_dev.py.
Test with: uv run pytest tests/unit/agents/
"""


@pytest.fixture
def sample_project_md_content() -> str:
    """Sample .sdd/project.md content for subtree scoping."""
    return """# Project context for src/bernstein

This is the core orchestration layer.
Run tests with: uv run pytest tests/unit/
"""


# -----------------------------------------------------------------------
# Test: deterministic receipts
# -----------------------------------------------------------------------


def test_deterministic_receipts(
    sample_role_content: str,
    sample_tasks_content: str,
    sample_lessons_content: str,
) -> None:
    """Same task graph spawned twice → identical context receipts.

    When the same context sections are passed to build_context_receipt,
    the resulting receipt must be byte-identical across multiple calls.
    This is the foundation of deterministic replay.
    """
    from bernstein.core.agents.context_receipt import build_context_receipt

    sections = [
        ("role", sample_role_content),
        ("tasks", sample_tasks_content),
        ("lessons", sample_lessons_content),
    ]

    receipt1 = build_context_receipt(sections)
    receipt2 = build_context_receipt(sections)

    assert receipt1 == receipt2
    assert receipt1.to_dict() == receipt2.to_dict()

    # Verify hashes are identical
    assert receipt1.entries[0].content_sha256 == receipt2.entries[0].content_sha256
    assert receipt1.entries[1].content_sha256 == receipt2.entries[1].content_sha256
    assert receipt1.entries[2].content_sha256 == receipt2.entries[2].content_sha256


def test_deterministic_receipt_with_same_content_different_order() -> None:
    """Order matters: different section order produces different receipts.

    The same content in different orders should produce different receipts
    because order is part of the prompt that the model receives.
    """
    from bernstein.core.agents.context_receipt import build_context_receipt

    sections_a = [("role", "role content"), ("tasks", "tasks content")]
    sections_b = [("tasks", "tasks content"), ("role", "role content")]

    receipt_a = build_context_receipt(sections_a)
    receipt_b = build_context_receipt(sections_b)

    # Different order → different receipts
    assert receipt_a != receipt_b
    assert receipt_a.entries[0].label == "role"
    assert receipt_b.entries[0].label == "tasks"
    assert receipt_a.entries[0].content_sha256 != receipt_b.entries[0].content_sha256


def test_deterministic_receipt_hash_is_stable() -> None:
    """Content hash is stable and reproducible."""
    from bernstein.core.agents.context_receipt import build_context_receipt

    content = "Stable content for hash verification."
    receipt = build_context_receipt([("section", content)])

    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert receipt.entries[0].content_sha256 == expected_hash

    # Calling again with same content produces same hash
    receipt2 = build_context_receipt([("section", content)])
    assert receipt.entries[0].content_sha256 == receipt2.entries[0].content_sha256


# -----------------------------------------------------------------------
# Test: scoping behavior (opt-in subtree context)
# -----------------------------------------------------------------------


def test_agents_md_scoping(tmp_path: Path, sample_agents_md_content: str) -> None:
    """Task targeting `src/bernstein/adapters/**` receives adapters context file.

    When scoping is enabled and a task owns files in a subtree with its own
    .sdd/project.md, that subtree's context file should be included in the receipt.

    This test verifies the lookup walks up from owned files to find scoped
    context files and includes them in the receipt.
    """
    from bernstein.core.agents.context_receipt import build_context_receipt
    from bernstein.core.agents.project_context import resolve_project_context
    from bernstein.core.tasks.models import Task

    # Create a fake project structure
    adapters_dir = tmp_path / "src" / "bernstein" / "adapters"
    adapters_dir.mkdir(parents=True)

    # Write scoped .sdd/project.md to adapters directory
    scoped_dir = adapters_dir / ".sdd"
    scoped_dir.mkdir(parents=True)
    scoped_file = scoped_dir / "project.md"
    scoped_file.write_text(sample_agents_md_content, encoding="utf-8")

    # Create a task that owns files in the adapters subtree
    task = Task(
        id="test-task-123",
        title="Test task",
        role="backend",
        description="Test task",
        owned_files=[str(adapters_dir / "claude.py")],
    )

    # Resolve project context for the task
    context = resolve_project_context([task], tmp_path)

    # The scoped context should be included
    assert "Adapter modules for CLI agent integration" in context

    # Build a receipt with the scoped context
    sections = [("project_context", context)]
    receipt = build_context_receipt(sections)

    assert len(receipt.entries) == 1
    assert receipt.entries[0].label == "project_context"
    assert "Adapter modules" in context


def test_nested_scoped_context_preferred(tmp_path: Path) -> None:
    """Nested scoped context takes precedence over root context.

    When both root .sdd/project.md and subtree .sdd/project.md exist,
    the subtree's context should be used (and supplement the root).
    """
    from bernstein.core.agents.project_context import resolve_project_context
    from bernstein.core.tasks.models import Task

    # Create project structure with both root and scoped context
    adapters_dir = tmp_path / "src" / "bernstein" / "adapters"
    adapters_dir.mkdir(parents=True)

    # Create directories first
    (tmp_path / ".sdd").mkdir(parents=True)
    (adapters_dir / ".sdd").mkdir(parents=True)

    root_project = tmp_path / ".sdd" / "project.md"
    root_project.write_text("# Root project context\nRoot content here.", encoding="utf-8")

    scoped_project = adapters_dir / ".sdd" / "project.md"
    scoped_project.write_text("# Adapters context\nScoped content here.", encoding="utf-8")

    # Task owned files in adapters subtree
    task = Task(
        id="test-task-456",
        title="Test task",
        role="backend",
        description="Test task",
        owned_files=[str(adapters_dir / "claude.py")],
    )

    context = resolve_project_context([task], tmp_path)

    # Scoped content should come first, then root
    assert "Scoped content here" in context
    assert "Root project context" in context
    # Scoped comes first
    assert context.index("Scoped content here") < context.index("Root project context")


def test_default_policy_noop(tmp_path: Path) -> None:
    """With scoping off (no scoped files), assembled bytes match current behavior.

    When there are no scoped context files, the behavior should be identical
    to the pre-scoping behavior (only root context is returned).
    """
    from bernstein.core.agents.project_context import resolve_project_context
    from bernstein.core.tasks.models import Task

    # Create only root context, no scoped files
    (tmp_path / ".sdd").mkdir(parents=True)
    root_project = tmp_path / ".sdd" / "project.md"
    root_content = "Root only context"
    root_project.write_text(root_content, encoding="utf-8")

    # Task owned files anywhere (no scoped context exists)
    task = Task(
        id="test-task-789",
        title="Test task",
        role="backend",
        description="Test task",
        owned_files=["some/file.py"],
    )

    context = resolve_project_context([task], tmp_path)

    # Should return only root context (pre-scoping behavior)
    assert context == root_content
    # No scoped context appended
    assert "Scoped" not in context


# -----------------------------------------------------------------------
# Test: receipt schema validation
# -----------------------------------------------------------------------


def test_receipt_schema_validation(
    sample_role_content: str,
    sample_tasks_content: str,
) -> None:
    """Receipt serializes/deserializes correctly with policy field.

    The ContextReceipt class must support round-trip serialization and
    deserialization, maintaining all fields correctly.
    """
    from bernstein.core.agents.context_receipt import ContextReceipt, build_context_receipt

    # Build a receipt
    receipt = build_context_receipt(
        [
            ("role", sample_role_content),
            ("tasks", sample_tasks_content),
        ]
    )

    # Serialize to dict
    receipt_dict = receipt.to_dict()

    # Verify all required fields are present
    assert "entries" in receipt_dict
    assert "total_token_estimate" in receipt_dict
    assert "total_chars" in receipt_dict
    assert "section_count" in receipt_dict

    # Verify entries structure
    assert isinstance(receipt_dict["entries"], list)
    assert len(receipt_dict["entries"]) == 2

    for entry_dict in receipt_dict["entries"]:
        assert "label" in entry_dict
        assert "content_sha256" in entry_dict
        assert "token_estimate" in entry_dict
        assert "char_count" in entry_dict

    # Deserialize from dict
    restored = ContextReceipt.from_dict(receipt_dict)

    # Verify round-trip integrity
    assert restored == receipt
    assert restored.to_dict() == receipt.to_dict()

    # Verify entry round-trip
    for original_entry, restored_entry in zip(receipt.entries, restored.entries, strict=True):
        assert original_entry.label == restored_entry.label
        assert original_entry.content_sha256 == restored_entry.content_sha256
        assert original_entry.token_estimate == restored_entry.token_estimate
        assert original_entry.char_count == restored_entry.char_count


def test_receipt_entry_schema_validation(sample_role_content: str) -> None:
    """ContextReceiptEntry serializes/deserializes correctly."""
    from bernstein.core.agents.context_receipt import ContextReceiptEntry

    entry = ContextReceiptEntry(
        label="role",
        content_sha256=hashlib.sha256(sample_role_content.encode("utf-8")).hexdigest(),
        token_estimate=10,
        char_count=len(sample_role_content),
    )

    # Serialize
    entry_dict = entry.to_dict()

    # Deserialize
    restored = ContextReceiptEntry.from_dict(entry_dict)

    # Verify
    assert restored == entry
    assert restored.to_dict() == entry.to_dict()


def test_receipt_empty_receipt_schema() -> None:
    """Empty receipt schema validates correctly."""
    from bernstein.core.agents.context_receipt import ContextReceipt, build_context_receipt

    receipt = build_context_receipt([])

    assert receipt.entries == []
    assert receipt.total_token_estimate == 0
    assert receipt.total_chars == 0
    assert receipt.section_count == 0

    # Round-trip
    receipt_dict = receipt.to_dict()
    restored = ContextReceipt.from_dict(receipt_dict)

    assert restored == receipt


def test_receipt_with_many_sections() -> None:
    """Receipt with many sections validates correctly."""
    from bernstein.core.agents.context_receipt import ContextReceipt, build_context_receipt

    sections = [(f"section_{i}", f"Content {i}") for i in range(10)]

    receipt = build_context_receipt(sections)

    assert receipt.section_count == 10
    assert len(receipt.entries) == 10
    assert receipt.total_chars == sum(len(f"Content {i}") for i in range(10))

    # Round-trip
    restored = ContextReceipt.from_dict(receipt.to_dict())
    assert restored == receipt


# -----------------------------------------------------------------------
# Test: integration with project context resolution
# -----------------------------------------------------------------------


def test_full_context_receipt_with_project_scoping(tmp_path: Path) -> None:
    """End-to-end: project context resolution → context receipt.

    This test verifies the complete flow from file-based project context
    to context receipt, including scoping.
    """
    from bernstein.core.agents.context_receipt import build_context_receipt
    from bernstein.core.agents.project_context import resolve_project_context
    from bernstein.core.tasks.models import Task

    # Create a complete project structure
    adapters_dir = tmp_path / "src" / "bernstein" / "adapters"
    adapters_dir.mkdir(parents=True)

    # Create directories first
    (tmp_path / ".sdd").mkdir(parents=True)
    (adapters_dir / ".sdd").mkdir(parents=True)

    root_project = tmp_path / ".sdd" / "project.md"
    root_project.write_text("# Root context", encoding="utf-8")

    scoped_project = adapters_dir / ".sdd" / "project.md"
    scoped_project.write_text("# Scoped context for adapters", encoding="utf-8")

    # Create a task in the adapters subtree
    task = Task(
        id="e2e-test-task",
        title="End-to-end test",
        role="backend",
        description="End-to-end test",
        owned_files=[str(adapters_dir / "claude.py")],
    )

    # Resolve context
    context = resolve_project_context([task], tmp_path)

    # Build receipt
    receipt = build_context_receipt([("project_context", context)])

    # Verify receipt structure
    assert len(receipt.entries) == 1
    assert receipt.entries[0].label == "project_context"
    assert receipt.entries[0].content_sha256 != ""  # Non-empty hash

    # Verify content includes scoped context
    assert "Scoped context for adapters" in context


def test_receipt_hashes_are_unique_per_content() -> None:
    """Different content produces different hashes (collision resistance)."""
    from bernstein.core.agents.context_receipt import build_context_receipt

    receipt1 = build_context_receipt([("section", "Content A")])
    receipt2 = build_context_receipt([("section", "Content B")])
    receipt3 = build_context_receipt([("section", "Content A")])  # Same as receipt1

    assert receipt1.entries[0].content_sha256 != receipt2.entries[0].content_sha256
    assert receipt1.entries[0].content_sha256 == receipt3.entries[0].content_sha256


def test_receipt_token_estimates_are_positive() -> None:
    """All token estimates are non-negative integers."""
    from bernstein.core.agents.context_receipt import build_context_receipt

    sections = [
        ("short", "a"),
        ("medium", "hello world " * 10),
        ("long", "hello world " * 100),
    ]

    receipt = build_context_receipt(sections)

    for entry in receipt.entries:
        assert entry.token_estimate >= 0
        assert isinstance(entry.token_estimate, int)

    assert receipt.total_token_estimate >= 0


# -----------------------------------------------------------------------
# Test: edge cases
# -----------------------------------------------------------------------


def test_receipt_with_unicode_content() -> None:
    """Receipt handles Unicode content correctly."""
    from bernstein.core.agents.context_receipt import ContextReceiptEntry, build_context_receipt

    unicode_content = "Hello 世界 🌍 中文"
    receipt = build_context_receipt([("unicode", unicode_content)])

    assert receipt.entries[0].content_sha256 != ""
    assert receipt.entries[0].char_count == len(unicode_content)

    # Round-trip preserves content
    entry_dict = receipt.entries[0].to_dict()
    restored = ContextReceiptEntry.from_dict(entry_dict)

    assert restored.char_count == len(unicode_content)


def test_receipt_with_empty_content() -> None:
    """Receipt handles empty content sections."""
    from bernstein.core.agents.context_receipt import build_context_receipt

    receipt = build_context_receipt([("empty", "")])

    assert len(receipt.entries) == 1
    assert receipt.entries[0].label == "empty"
    assert receipt.entries[0].char_count == 0

    # Empty string still has a hash
    assert receipt.entries[0].content_sha256 != ""


def test_receipt_with_special_characters() -> None:
    """Receipt handles special characters correctly."""
    from bernstein.core.agents.context_receipt import build_context_receipt

    special_content = 'Special: "quotes" and \\backslash and \n newlines'
    receipt = build_context_receipt([("special", special_content)])

    assert receipt.entries[0].content_sha256 != ""
    assert receipt.entries[0].char_count == len(special_content)


def test_receipt_order_preservation() -> None:
    """Receipt preserves section order."""
    from bernstein.core.agents.context_receipt import ContextReceipt, build_context_receipt

    sections = [
        ("zeta", "last alphabetically"),
        ("alpha", "first alphabetically"),
        ("mid", "middle"),
    ]

    receipt = build_context_receipt(sections)

    assert [e.label for e in receipt.entries] == ["zeta", "alpha", "mid"]

    # Round-trip preserves order
    restored = ContextReceipt.from_dict(receipt.to_dict())
    assert [e.label for e in restored.entries] == ["zeta", "alpha", "mid"]
