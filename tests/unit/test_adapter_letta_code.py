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


def test_spawn_passes_new_agent_without_a_conversation(tmp_path: Path) -> None:
    """letta 0.26 refuses ``--conversation`` beside ``--new-agent`` and exits 1
    before the prompt runs ("--conversation cannot be used with --new-agent"),
    so passing both made every spawn fail."""
    adapter = LettaCodeAdapter()
    proc_mock = make_popen_mock(900)

    with patch("bernstein.adapters.letta_code.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="letta-s1",
        )

    inner = inner_cmd(popen.call_args.args[0])
    assert "--new-agent" in inner
    assert "--conversation" not in inner
    assert "--conv" not in inner
    assert inner[inner.index("-p") + 1] == "fix the bug"


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


def test_fresh_sessions_keep_distinct_memory_exports_and_conversation_metadata(tmp_path: Path) -> None:
    """Fresh sessions must keep their own export file and the CLI's conversation ID."""
    adapter = LettaCodeAdapter()
    sessions = (("letta-s1", "agent-77", "conv-77"), ("letta-s2", "agent-88", "conv-88"))

    with patch("bernstein.adapters.letta_code.export_memory_digest", return_value="d" * 64) as export:
        for session_id, agent_id, conversation_id in sessions:
            log_path = tmp_path / f"{session_id}.log"
            envelope = {**ENVELOPE, "agent_id": agent_id, "conversation_id": conversation_id}
            log_path.write_text(json.dumps(envelope) + "\n")
            result = SpawnResult(pid=900, log_path=log_path)

            adapter._post_exit(make_popen_mock(900), tmp_path, session_id, log_path, result)

            meta_path = tmp_path / ".sdd" / "runtime" / f"{session_id}.letta_meta.json"
            metadata = json.loads(meta_path.read_text())
            assert metadata["agent_id"] == agent_id
            assert metadata["conversation_id"] == conversation_id
            assert result.finish_reason == ""

    export_paths = [call.args[2] for call in export.call_args_list]
    assert len(export_paths) == len(set(export_paths)) == 2
    assert {path.name for path in export_paths} == {"letta_memory_letta-s1.json", "letta_memory_letta-s2.json"}
    assert all(path.with_suffix(".json.sha256").read_text() == "d" * 64 for path in export_paths)


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
