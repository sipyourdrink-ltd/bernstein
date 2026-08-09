"""Unit tests for #2959: Generic Python-invoked agent-runtime adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import (
    STRATEGY_MATRIX,
    DangerousModeStrategy,
    EventChannel,
    OutputMode,
    ResumeStrategy,
    undeclared_strategies,
)
from bernstein.adapters.python_runtime import PythonRuntimeAdapter
from bernstein.adapters.registry import get_adapter, selectable_adapter_names


def test_python_runtime_adapter_registered_and_strategy_matrix() -> None:
    assert "python_runtime" in selectable_adapter_names()
    adapter = get_adapter("python_runtime")
    assert isinstance(adapter, PythonRuntimeAdapter)

    strategy = STRATEGY_MATRIX.get("python_runtime")
    assert strategy is not None
    assert strategy.resume == ResumeStrategy.UNSUPPORTED
    assert strategy.dangerous_mode == DangerousModeStrategy.ALWAYS_ON
    assert strategy.event_channel == EventChannel.STREAM_JSON
    assert strategy.output_mode == OutputMode.GIT_DIFF

    assert "python_runtime" not in undeclared_strategies(selectable_adapter_names())


def test_python_runtime_plugin_info() -> None:
    adapter = PythonRuntimeAdapter()
    info = adapter.plugin_info()
    assert info.name == "python_runtime"
    assert info.version == "1.0.0"


def test_python_runtime_spawn(tmp_path: Path) -> None:
    adapter = PythonRuntimeAdapter()
    model_cfg = ModelConfig(model="gpt-4o", effort="normal")

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        result = adapter.spawn(
            prompt="Run custom python agent",
            workdir=tmp_path,
            model_config=model_cfg,
            session_id="py-task-1",
            mcp_config={"runtime_module": "custom_agent", "runtime_entrypoint": "run_agent"},
            timeout_seconds=0,
        )

        assert result.pid == 9999
        assert mock_popen.called

        cmd_list = mock_popen.call_args[0][0]
        assert "python_runtime_runner.py" in str(cmd_list)
        assert "--prompt" in cmd_list
        assert "Run custom python agent" in cmd_list
        assert "--runtime-module" in cmd_list
        assert "custom_agent" in cmd_list
        assert "--runtime-entrypoint" in cmd_list
        assert "run_agent" in cmd_list
