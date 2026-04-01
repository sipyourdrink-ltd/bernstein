"""Tests for {{ cookiecutter.adapter_name }} adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import SpawnResult
from {{ cookiecutter.adapter_name }} import {{ cookiecutter.adapter_class }}


@pytest.fixture()
def adapter(tmp_path: Path) -> {{ cookiecutter.adapter_class }}:
    """Create adapter instance for testing."""
    return {{ cookiecutter.adapter_class }}(
        workdir=tmp_path,
        session_id="test-session-123",
    )


class Test{{ cookiecutter.adapter_class }}:
    """Test suite for {{ cookiecutter.adapter_class }}."""

    def test_spawn_success(self, adapter: {{ cookiecutter.adapter_class }}, tmp_path: Path) -> None:
        """Test successful agent spawn."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Success output",
                stderr="",
            )

            result = adapter.spawn(
                task_description="Test task",
                files=["file1.py"],
                model="test-model",
            )

            assert result.success is True
            assert result.output == "Success output"
            assert result.error == ""

    def test_spawn_timeout(self, adapter: {{ cookiecutter.adapter_class }}, tmp_path: Path) -> None:
        """Test agent spawn timeout."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=60)

            result = adapter.spawn(
                task_description="Test task",
                timeout=60,
            )

            assert result.success is False
            assert "Timeout" in result.error

    def test_spawn_failure(self, adapter: {{ cookiecutter.adapter_class }}, tmp_path: Path) -> None:
        """Test agent spawn failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Error occurred",
            )

            result = adapter.spawn(task_description="Test task")

            assert result.success is False
            assert result.error == "Error occurred"

    def test_detect_tier(self, adapter: {{ cookiecutter.adapter_class }}) -> None:
        """Test tier detection."""
        # Template - implement based on your adapter
        tier = adapter.detect_tier()
        # Assert expected tier detection behavior
        assert tier is None  # Template default
