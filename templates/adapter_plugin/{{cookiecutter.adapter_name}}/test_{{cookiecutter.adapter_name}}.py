"""Tests for the {{ cookiecutter.adapter_name }} adapter."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import ModelConfig

import {{ cookiecutter.adapter_name }} as adapter_module
from {{ cookiecutter.adapter_name }} import {{ cookiecutter.adapter_class }}

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def adapter() -> {{ cookiecutter.adapter_class }}:
    """Create an adapter instance for testing."""
    return {{ cookiecutter.adapter_class }}()


@pytest.fixture()
def model_config() -> ModelConfig:
    """A model selection as the orchestrator passes it."""
    return ModelConfig(model="test-model", effort="medium")


class Test{{ cookiecutter.adapter_class }}:
    """Test suite for {{ cookiecutter.adapter_class }}."""

    def test_is_a_cli_adapter_with_a_name(self, adapter: {{ cookiecutter.adapter_class }}) -> None:
        """The registry accepts the class and can show its name."""
        assert isinstance(adapter, CLIAdapter)
        assert adapter.name() == "{{ cookiecutter.adapter_class }}"

    def test_build_command_passes_prompt_and_model(
        self, adapter: {{ cookiecutter.adapter_class }}, model_config: ModelConfig
    ) -> None:
        """The argv names the binary, the prompt and the model."""
        cmd = adapter.build_command("Fix the bug", model_config)

        assert cmd[0] == adapter_module.CLI_BINARY
        assert "Fix the bug" in cmd
        assert cmd[cmd.index("--model") + 1] == "test-model"

    def test_spawn_returns_pid_and_log(
        self,
        adapter: {{ cookiecutter.adapter_class }},
        model_config: ModelConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """spawn launches the process and returns without waiting for it."""
        # Stand in for the real CLI with a process that prints one line.
        monkeypatch.setattr(
            adapter,
            "build_command",
            lambda prompt, _model: [sys.executable, "-c", f"print({prompt!r})"],
        )

        result = adapter.spawn(
            prompt="hello from the agent",
            workdir=tmp_path,
            model_config=model_config,
            session_id="backend-abc123",
            timeout_seconds=0,
        )

        assert isinstance(result, SpawnResult)
        assert result.pid > 0
        assert result.log_path == tmp_path / ".sdd" / "runtime" / "backend-abc123.log"
        result.proc.wait(timeout=30)  # type: ignore[attr-defined]
        assert "hello from the agent" in result.log_path.read_text()

    def test_spawn_reports_a_missing_binary(
        self,
        adapter: {{ cookiecutter.adapter_class }},
        model_config: ModelConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A CLI that is not installed is a clear error, not a traceback from Popen."""
        monkeypatch.setattr(adapter, "build_command", lambda _prompt, _model: ["no-such-binary-for-this-test"])

        with pytest.raises(RuntimeError, match="not found in PATH"):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=model_config,
                session_id="backend-abc123",
                timeout_seconds=0,
            )
