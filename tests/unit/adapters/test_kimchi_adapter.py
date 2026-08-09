"""Unit tests for #3100: Kimchi CLI adapter driven over ACP event channel."""

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
from bernstein.adapters.conformance import replay_acp_event_fixture
from bernstein.adapters.kimchi import KimchiAdapter
from bernstein.adapters.registry import get_adapter, selectable_adapter_names


def test_kimchi_adapter_registered_and_strategy_matrix_matches() -> None:
    assert "kimchi" in selectable_adapter_names()
    adapter = get_adapter("kimchi")
    assert isinstance(adapter, KimchiAdapter)

    strategy = STRATEGY_MATRIX.get("kimchi")
    assert strategy is not None
    assert strategy.resume == ResumeStrategy.FLAG
    assert strategy.dangerous_mode == DangerousModeStrategy.CLI_FLAG
    assert strategy.event_channel == EventChannel.ACP
    assert strategy.output_mode == OutputMode.GIT_DIFF

    # Assert no undeclared adapter strategies across the repo
    assert "kimchi" not in undeclared_strategies(selectable_adapter_names())


def test_kimchi_spawn_drives_acp_mode_and_env_isolation(tmp_path: Path) -> None:
    adapter = KimchiAdapter()
    model_cfg = ModelConfig(model="open-weight-7b", effort="normal")

    with (
        patch("subprocess.Popen") as mock_popen,
        patch.dict("os.environ", {"KIMCHI_API_KEY": "secret-key", "SECRET_VAR": "leave-me-out"}),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        result = adapter.spawn(
            prompt="Refactor auth module",
            workdir=tmp_path,
            model_config=model_cfg,
            session_id="kimchi-task-1",
            dangerous_mode=True,
            timeout_seconds=0,
        )

        assert result.pid == 12345
        assert mock_popen.called

        # Inspect command line
        call_args = mock_popen.call_args
        # Popen receives wrapped_cmd as first positional arg
        cmd_list = call_args[0][0]
        assert "--mode" in cmd_list
        assert "acp" in cmd_list
        assert "--yolo" in cmd_list
        assert "--prompt" in cmd_list

        # Inspect environment isolation & pinned telemetry
        env = call_args[1].get("env", {})
        assert env.get("KIMCHI_API_KEY") == "secret-key"
        assert env.get("KIMCHI_TELEMETRY_ENABLED") == "0"
        assert "SECRET_VAR" not in env


def test_kimchi_resume_session_flag(tmp_path: Path) -> None:
    adapter = KimchiAdapter()
    model_cfg = ModelConfig(model="open-weight-7b", effort="normal")
    session_file = tmp_path / "session.json"

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12346
        mock_popen.return_value = mock_proc

        adapter.spawn(
            prompt="Continue task",
            workdir=tmp_path,
            model_config=model_cfg,
            session_id="kimchi-task-2",
            session_path=session_file,
            timeout_seconds=0,
        )

        cmd_list = mock_popen.call_args[0][0]
        assert "--session" in cmd_list
        assert str(session_file) in cmd_list


def test_acp_fixture_replay_parity(tmp_path: Path) -> None:
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "acp" / "lifecycle" / "kimchi_acp_task.jsonl"
    dir_1 = tmp_path / "sdd1"
    dir_2 = tmp_path / "sdd2"

    res1 = replay_acp_event_fixture(fixture_path, sdd_dir=dir_1)
    res2 = replay_acp_event_fixture(fixture_path, sdd_dir=dir_2)

    assert res1.ok is True
    assert res2.ok is True
    assert res1.journal_head == res2.journal_head


def test_acp_stdout_drift_insensitivity(tmp_path: Path) -> None:
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "acp" / "lifecycle" / "kimchi_acp_task.jsonl"
    clean_dir = tmp_path / "clean_sdd"
    drift_dir = tmp_path / "drift_sdd"

    # Clean replay
    clean_res = replay_acp_event_fixture(fixture_path, sdd_dir=clean_dir)

    # Interleave arbitrary non-ACP lines on stdout
    raw_lines = fixture_path.read_text().splitlines()
    drift_lines = [
        "Kimchi v0.1.74 (c) 2026",
        raw_lines[0],
        "[progress] Loading model weights...",
        raw_lines[1],
        "\033[32mOK\033[0m",
        raw_lines[2],
        raw_lines[3],
    ]

    from bernstein.adapters.acp_channel import run_acp_channel
    from bernstein.core.replay.journal import EventJournal

    journal = EventJournal("acp-conf-drift", drift_dir)
    json_lines = (line for line in drift_lines if line.strip().startswith("{"))
    drift_res = run_acp_channel(json_lines, journal=journal, session_id="kimchi-s1")

    assert clean_res.ok is True
    assert drift_res.ok is True
    assert clean_res.journal_head == journal.head()
