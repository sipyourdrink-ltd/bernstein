"""Tests for child task context inheritance."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.knowledge_base import TaskContextBuilder
from bernstein.core.models import Task


def test_child_task_inherits_parent_context(tmp_path: Path) -> None:
    """Test that a child task inherits owned_files from its parent."""
    builder = TaskContextBuilder(tmp_path)

    parent = Task(id="p1", title="Parent", description="...", role="backend", owned_files=["auth.py"])
    child = Task(
        id="c1", title="Child", description="...", role="backend", parent_task_id="p1", owned_files=["main.py"]
    )

    # Mock store
    store = MagicMock()
    store.get_task.return_value = parent

    # We mock ContextCompressor to avoid real compression logic
    with patch("bernstein.core.context_compression.ContextCompressor.compress") as mock_compress:
        mock_compress.side_effect = Exception("Fallback to uncompressed")

        # We need to mock task_context because it calls file_context which checks filesystem
        with patch.object(builder, "task_context") as mock_task_ctx:
            mock_task_ctx.return_value = "mocked task context"
            builder.build_context([child], store=store)

            # Check that child now has both files
            assert "auth.py" in child.owned_files
            assert "main.py" in child.owned_files

            # Verify mock_task_ctx was called with inherited files
            call_args = mock_task_ctx.call_args[0][0]
            assert "auth.py" in call_args
            assert "main.py" in call_args
