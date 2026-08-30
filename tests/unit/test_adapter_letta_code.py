"""Unit tests for LettaCodeAdapter's constructed argv.

Tests verify the spawn command flags and metadata sidecar output.
Each test MUST fail against current adapter and pass after tasks 3671-A/B
are applied.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import SpawnResult
from bernstein.adapters.letta_code import MEMORY_EXPORT_FAILURE, LettaCodeAdapter, export_memory_digest
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
    assert inner[:3] == ["letta", "--output-format", "stream-json"]


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


ENVELOPE = {
    "agent_id": "agent-77",
    "conversation_id": "conv-77",
    "token_usage": {"input": 10, "output": 5},
}


def _log_with_envelope(tmp_path: Path) -> Path:
    """A stream-json log carrying one envelope line, as the CLI emits it."""
    log_path = tmp_path / "letta.log"
    log_path.write_text('{"type":"chatter"}\n' + json.dumps(ENVELOPE) + "\n")
    return log_path


def test_export_memory_digest_hashes_the_exported_file(tmp_path: Path) -> None:
    """The digest is the SHA-256 of what the export actually wrote."""
    export_path = tmp_path / "memory.json"
    proc = make_popen_mock(901)
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0

    def fake_popen(cmd, **kwargs):
        export_path.write_bytes(b'{"blocks": []}')
        return proc

    with patch("bernstein.adapters.letta_code.subprocess.Popen", side_effect=fake_popen):
        digest = export_memory_digest(tmp_path, "agent-77", export_path)

    assert digest == hashlib.sha256(b'{"blocks": []}').hexdigest()


def test_export_memory_digest_raises_when_the_cli_fails(tmp_path: Path) -> None:
    proc = make_popen_mock(901)
    proc.communicate.return_value = (b"", b"no such agent")
    proc.returncode = 2

    with (
        patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc),
        pytest.raises(RuntimeError, match="no such agent"),
    ):
        export_memory_digest(tmp_path, "agent-77", tmp_path / "memory.json")


def test_export_memory_digest_raises_when_the_cli_writes_nothing(tmp_path: Path) -> None:
    """A zero exit with no file is a failure, not an empty memory."""
    proc = make_popen_mock(901)
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0

    with (
        patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc),
        pytest.raises(RuntimeError, match="wrote no file"),
    ):
        export_memory_digest(tmp_path, "agent-77", tmp_path / "memory.json")


def test_spawn_publishes_the_post_exit_worker(tmp_path: Path) -> None:
    """The sidecars are written off-thread, so the caller gets the worker to join.

    Regression: the thread was started and dropped, leaving no way to tell a
    session whose bookkeeping had finished from one still mid-write.
    """
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock):
        result = adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    assert result.post_exit_thread is not None
    result.post_exit_thread.join(timeout=10)
    assert not result.post_exit_thread.is_alive()


def test_post_exit_records_meta_sidecar(tmp_path: Path) -> None:
    """The envelope parsed out of the log lands in .sdd/runtime/<session>.letta_meta.json."""
    adapter = LettaCodeAdapter()
    result = SpawnResult(pid=900, log_path=_log_with_envelope(tmp_path))

    with patch("bernstein.adapters.letta_code.export_memory_digest", return_value="d" * 64):
        adapter._post_exit(make_popen_mock(900), tmp_path, "letta-s1", result.log_path, result)

    meta_path = tmp_path / ".sdd" / "runtime" / "letta-s1.letta_meta.json"
    assert meta_path.exists(), f"Meta sidecar not found at {meta_path}"
    data = json.loads(meta_path.read_text())
    assert data["agent_id"] == "agent-77"
    assert data["conversation_id"] == "conv-77"


def test_post_exit_records_memory_digest(tmp_path: Path) -> None:
    """The digest returned by the export lands in the .sha256 sidecar beside it."""
    adapter = LettaCodeAdapter()
    result = SpawnResult(pid=900, log_path=_log_with_envelope(tmp_path))

    with patch("bernstein.adapters.letta_code.export_memory_digest", return_value="abc123digest"):
        adapter._post_exit(make_popen_mock(900), tmp_path, "letta-s1", result.log_path, result)

    runtime_dir = tmp_path / ".sdd" / "runtime"
    sidecars = list(runtime_dir.glob("letta_memory_*.json.sha256"))
    assert len(sidecars) == 1, f"expected one digest sidecar, found {sidecars}"
    assert sidecars[0].read_text() == "abc123digest"
    assert result.finish_reason == ""


def test_post_exit_reports_memory_export_failure(tmp_path: Path, caplog) -> None:
    """A failed export is named in finish_reason and does not cost us the envelope."""
    adapter = LettaCodeAdapter()
    result = SpawnResult(pid=900, log_path=_log_with_envelope(tmp_path))

    with (
        patch(
            "bernstein.adapters.letta_code.export_memory_digest",
            side_effect=RuntimeError("Memory export failed"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        adapter._post_exit(make_popen_mock(900), tmp_path, "letta-s1", result.log_path, result)

    assert "Memory export failed" in caplog.text
    assert result.finish_reason == MEMORY_EXPORT_FAILURE
    # The metadata is the record of the run; an export that failed must not take it down.
    assert (tmp_path / ".sdd" / "runtime" / "letta-s1.letta_meta.json").exists()
    assert not list((tmp_path / ".sdd" / "runtime").glob("*.sha256"))


def test_strategy_override_matches_matrix(tmp_path: Path) -> None:
    """After tasks 3671-A/B: assert adapter.strategy() == STRATEGY_MATRIX[letta_code]."""
    from bernstein.adapters._contract import STRATEGY_MATRIX

    adapter = LettaCodeAdapter()
    expected = STRATEGY_MATRIX["letta_code"]
    actual = adapter.strategy()
    assert actual == expected
