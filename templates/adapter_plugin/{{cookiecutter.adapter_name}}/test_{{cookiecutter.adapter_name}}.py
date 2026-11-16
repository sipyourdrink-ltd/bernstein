"""Tests for {{ cookiecutter.adapter_name }} adapter."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig
from {{ cookiecutter.adapter_name }} import {{ cookiecutter.adapter_class }}

from bernstein.adapters.base import SpawnResult

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def adapter() -> {{ cookiecutter.adapter_class }}:
    """Create an adapter as entry-point discovery does."""
    return {{ cookiecutter.adapter_class }}()


class Test{{ cookiecutter.adapter_class }}:
    """Test the launch contract without invoking a real agent."""

    def test_name(self, adapter: {{ cookiecutter.adapter_class }}) -> None:
        assert adapter.name() == "{{ cookiecutter.adapter_name }}"

    def test_spawn_success(self, adapter: {{ cookiecutter.adapter_class }}, tmp_path: Path) -> None:
        """Return a live process handle, without waiting or collecting output."""
        with patch("{{ cookiecutter.adapter_name }}.subprocess.Popen") as mock_popen:
            proc = mock_popen.return_value
            proc.pid = 12345

            result = adapter.spawn(
                prompt="Test task",
                workdir=tmp_path,
                model_config=ModelConfig(model="test-model", effort="high"),
                session_id="test-session",
                mcp_config=None,
                timeout_seconds=0,
                task_scope="small",
                budget_multiplier=1.0,
                system_addendum="",
                multimodal_context=None,
            )

            assert isinstance(result, SpawnResult)
            assert result.pid == 12345
            assert result.proc is proc
            assert result.log_path == tmp_path / ".bernstein-test-session.log"
            assert result.log_path.is_file()
            assert result.timeout_timer is None
            cmd = mock_popen.call_args.args[0]
            assert cmd == [
                "{{ cookiecutter.adapter_name }}",
                "run",
                "--description",
                "Test task",
                "--model",
                "test-model",
                "--effort",
                "high",
            ]
            kwargs = mock_popen.call_args.kwargs
            assert kwargs["cwd"] == tmp_path
            assert kwargs["stdout"].name == str(result.log_path)
            assert kwargs["stdout"].closed
            assert kwargs["stderr"] == subprocess.STDOUT
            assert kwargs["start_new_session"] == (os.name == "posix")
            proc.wait.assert_not_called()
            proc.communicate.assert_not_called()

    def test_spawn_timeout(self, adapter: {{ cookiecutter.adapter_class }}, tmp_path: Path) -> None:
        """Timeouts use the asynchronous watchdog, not a blocking run call."""
        timer = MagicMock()
        with (
            patch("{{ cookiecutter.adapter_name }}.subprocess.Popen") as mock_popen,
            patch.object(adapter, "_start_timeout_watchdog", return_value=timer) as watchdog,
        ):
            mock_popen.return_value.pid = 12345
            result = adapter.spawn(
                prompt="Test task",
                workdir=tmp_path,
                model_config=ModelConfig(model="", effort=""),
                session_id="timed-session",
                timeout_seconds=60,
            )

            assert result.timeout_timer is timer
            watchdog.assert_called_once_with(12345, 60, "timed-session")

    @pytest.mark.parametrize("error", [FileNotFoundError("missing CLI"), PermissionError("not executable")])
    def test_spawn_failure(
        self,
        adapter: {{ cookiecutter.adapter_class }},
        tmp_path: Path,
        error: OSError,
    ) -> None:
        """Launch errors must propagate instead of fabricating a result."""
        with (
            patch("{{ cookiecutter.adapter_name }}.subprocess.Popen", side_effect=error) as mock_popen,
            pytest.raises(type(error), match=str(error)),
        ):
            adapter.spawn(
                prompt="Test task",
                workdir=tmp_path,
                model_config=ModelConfig(model="", effort=""),
                session_id="failed-session",
            )
        assert mock_popen.call_args.kwargs["stdout"].closed

    def test_log_failure(self, adapter: {{ cookiecutter.adapter_class }}, tmp_path: Path) -> None:
        """A log-file error must prevent launching the CLI."""
        with (
            patch("{{ cookiecutter.adapter_name }}.subprocess.Popen") as mock_popen,
            pytest.raises(FileNotFoundError),
        ):
            adapter.spawn(
                prompt="Test task",
                workdir=tmp_path / "missing",
                model_config=ModelConfig(model="", effort=""),
                session_id="failed-session",
            )
        mock_popen.assert_not_called()

    def test_detect_tier(self, adapter: {{ cookiecutter.adapter_class }}) -> None:
        assert adapter.detect_tier() is None
