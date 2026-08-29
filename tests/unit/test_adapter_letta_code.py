"""Unit tests for LettaCodeAdapter's constructed argv.

Tests verify the spawn command flags and metadata sidecar output.
Each test MUST fail against current adapter and pass after tasks 3671-A/B
are applied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.letta_code import LettaCodeAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert inner[:3] == ["letta", "--yolo", "-p"]
    assert inner[-1] == "fix the bug"


def test_spawn_uses_stream_json_format(tmp_path: Path) -> None:
    """After tasks 3671-A/B: inner command has --output-format stream-json."""
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert "--output-format" in inner
    assert inner[inner.index("--output-format") + 1] == "stream-json"


def test_spawn_uses_permission_mode_not_yolo(tmp_path: Path) -> None:
    """After tasks 3671-A/B: no --yolo, has --permission-mode unrestricted."""
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert "--yolo" not in inner
    assert "--permission-mode" in inner
    assert inner[inner.index("--permission-mode") + 1] == "unrestricted"


def test_spawn_passes_new_agent_and_conversation(tmp_path: Path) -> None:
    """After tasks 3671-A/B: --new-agent and --conversation <derived_id> in command."""
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    cmd = popen.call_args.args[0]
    inner = inner_cmd(cmd)
    assert "--new-agent" in inner
    assert "--conversation" in inner


def test_consecutive_runs_get_distinct_conversation_bindings(tmp_path: Path) -> None:
    """After tasks 3671-A/B: two spawns with different session_ids produce different --conversation values."""
    adapter = LettaCodeAdapter()

    proc_mock1 = make_popen_mock(900)
    proc_mock2 = make_popen_mock(901)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", side_effect=[proc_mock1, proc_mock2]) as popen_calls:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s2",
        )

    cmd1 = popen_calls.call_args_list[0][0][0]
    cmd2 = popen_calls.call_args_list[1][0][0]
    inner1 = inner_cmd(cmd1)
    inner2 = inner_cmd(cmd2)
    conv1 = inner1[inner1.index("--conversation") + 1]
    conv2 = inner2[inner2.index("--conversation") + 1]
    assert conv1 != conv2


def test_spawn_records_meta_sidecar(tmp_path: Path) -> None:
    """After tasks 3671-A/B: .sdd/runtime/<session_id>.letta_meta.json written with agent_id and conversation_id."""
    import json
    from pathlib import Path

    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        result = adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    meta_path = tmp_path / ".sdd" / "runtime" / "letta-s1.letta_meta.json"
    assert meta_path.exists(), f"Meta sidecar not found at {meta_path}"
    with open(meta_path) as f:
        data = json.load(f)
    assert "agent_id" in data, f"Missing agent_id in {meta_path}"
    assert "conversation_id" in data, f"Missing conversation_id in {meta_path}"


def test_memory_export_digest_recorded(tmp_path: Path, monkeypatch) -> None:
    """After tasks 3671-A/B: mock letta memory export success, assert digest written."""
    import json

    def mock_export(*args, **kwargs):
        return {"agent_id": "test-agent-123", "conversation_id": "test-conv-456", "digest": "abc123digest"}

    monkeypatch.setattr(
        "bernstein.adapters.letta_code.export_memory_digest", mock_export,
    )

    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock):
        result = adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    digest_path = tmp_path / ".sdd" / "runtime" / "pids" / "digest.json"
    assert digest_path.exists(), f"Digest not found at {digest_path}"
    with open(digest_path) as f:
        data = json.load(f)
    assert data["digest"] == "abc123digest"


def test_memory_export_failure_reported(tmp_path: Path, monkeypatch, caplog) -> None:
    """After tasks 3671-A/B: mock export failure, assert warning logged and finish_reason set."""
    import json
    import logging

    def mock_export_failure(*args, **kwargs):
        raise RuntimeError("Memory export failed")

    monkeypatch.setattr(
        "bernstein.adapters.letta_code.export_memory_digest", mock_export_failure,
    )

    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock):
        with caplog.at_level(logging.WARNING):
            result = adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="letta-s1",
            )

    assert "Memory export failed" in caplog.text
    assert result.finish_reason == "memory_export_failure"


def test_strategy_override_matches_matrix(tmp_path: Path) -> None:
    """After tasks 3671-A/B: assert adapter.strategy() == STRATEGY_MATRIX[letta_code]."""
    from bernstein.adapters._contract import STRATEGY_MATRIX

    adapter = LettaCodeAdapter()
    expected = STRATEGY_MATRIX["letta_code"]
    actual = adapter.strategy()
    assert actual == expected