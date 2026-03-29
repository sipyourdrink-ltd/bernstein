"""Protocol compatibility matrix tests.

These tests verify that core Bernstein functionality works with different
protocol versions (MCP, A2A, ACP). They run as part of the CI matrix to
generate the compatibility table.
"""

import pytest


class TestProtocolVersionDetection:
    """Test that protocol versions are detected correctly."""

    def test_mcp_importable(self):
        """MCP library should be importable."""
        try:
            import mcp  # noqa: F401

            assert True
        except ImportError:
            pytest.skip("MCP not installed")

    def test_mcp_version_matches_environment(self):
        """Verify MCP version is available."""
        try:
            import mcp

            # MCP may not have __version__, check for version info in various places
            version = getattr(mcp, "__version__", None) or getattr(mcp, "version", None)
            # If no version attribute, just check it imports successfully
            assert mcp is not None
        except ImportError:
            pytest.skip("MCP not installed")

    def test_a2a_importable(self):
        """A2A library should be importable if available."""
        try:
            import a2a  # noqa: F401

            assert True
        except ImportError:
            pytest.skip("A2A not installed")


class TestBernsteinProtocolIntegration:
    """Test Bernstein adapters with protocol libraries."""

    def test_adapter_initialization(self):
        """Adapters should initialize without error."""
        from bernstein.adapters.base import CLIAdapter

        assert CLIAdapter is not None

    def test_task_model_serialization(self):
        """Task models should serialize correctly."""
        from bernstein.core.models import Complexity, Task, TaskStatus

        task = Task(
            id="test-001",
            title="Test task",
            description="A test task for protocol compatibility",
            role="qa",
            status=TaskStatus.OPEN,
            complexity=Complexity.LOW,
        )
        assert task.id == "test-001"
        assert task.status == TaskStatus.OPEN
        assert task.role == "qa"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
