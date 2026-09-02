"""Unit tests for ManagerAdapter (adapters/manager.py)."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import ModelConfig
from bernstein.core.platform_compat import ProcessReapReceipt

from bernstein.adapters.manager import ManagerAdapter

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter


# Above every platform's pid ceiling, so the reap path can probe it with a
# signal-0 existence check and always observe nothing.
_UNUSED_PGID = 4_000_000


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------


class TestSpawn:
    def _spawn(
        self,
        tmp_path: Path,
        prompt: str = "do something (id=abc-123)",
        session_id: str = "sess-1",
    ) -> tuple[MagicMock, MagicMock]:
        adapter = ManagerAdapter()
        proc_mock = _make_popen_mock(pid=1234)

        with patch("bernstein.adapters.manager.subprocess.Popen", return_value=proc_mock) as popen:
            result = adapter.spawn(
                prompt=prompt,
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id=session_id,
            )

        return popen, result  # type: ignore[return-value]

    @staticmethod
    def _inner_cmd(full_cmd: list[str]) -> list[str]:
        """Extract the inner command after the '--' worker separator."""
        sep = full_cmd.index("--")
        return full_cmd[sep + 1 :]

    def test_cmd_wrapped_with_worker(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        assert "--role" in cmd
        assert "--session" in cmd

    def test_inner_cmd_uses_manager_module(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        inner = self._inner_cmd(popen.call_args.args[0])
        assert inner[0] == sys.executable
        assert inner[1:3] == ["-m", "bernstein.core.orchestration.manager"]

    def test_cmd_includes_port_8052(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        inner = self._inner_cmd(popen.call_args.args[0])
        assert "--port" in inner
        assert inner[inner.index("--port") + 1] == "8052"

    def test_cmd_extracts_task_id_from_prompt(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path, prompt="Fix bug (id=task-xyz-789)")
        inner = self._inner_cmd(popen.call_args.args[0])
        assert "--task-id" in inner
        assert inner[inner.index("--task-id") + 1] == "task-xyz-789"

    def test_cmd_fallback_task_id_when_no_match(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path, prompt="No task id here")
        inner = self._inner_cmd(popen.call_args.args[0])
        assert "--task-id" in inner
        assert inner[inner.index("--task-id") + 1] == "task-000"

    def test_start_new_session_true(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True

    def test_cwd_is_workdir(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        kwargs = popen.call_args.kwargs
        assert kwargs.get("cwd") == tmp_path

    def test_env_is_provided(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        kwargs = popen.call_args.kwargs
        env = kwargs.get("env")
        assert env is not None
        assert isinstance(env, dict)

    def test_log_file_created(self, tmp_path: Path) -> None:
        adapter = ManagerAdapter()
        proc_mock = _make_popen_mock(pid=42)

        with patch("bernstein.adapters.manager.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="task (id=t1)",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="my-session",
            )

        assert result.log_path == tmp_path / ".sdd" / "runtime" / "my-session.log"
        assert result.log_path.exists()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = ManagerAdapter()
        proc_mock = _make_popen_mock(pid=9999)

        with patch("bernstein.adapters.manager.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="(id=t2)",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="s2",
            )

        assert result.pid == 9999

    def test_stderr_redirected_to_stdout(self, tmp_path: Path) -> None:
        popen, _ = self._spawn(tmp_path)
        kwargs = popen.call_args.kwargs
        assert kwargs.get("stderr") == subprocess.STDOUT


# ---------------------------------------------------------------------------
# is_alive()
# ---------------------------------------------------------------------------


class TestIsAlive:
    def test_true_when_process_alive(self) -> None:
        adapter = ManagerAdapter()
        with patch("bernstein.adapters.manager.process_alive", return_value=True):
            assert adapter.is_alive(1234) is True

    def test_false_when_process_not_alive(self) -> None:
        adapter = ManagerAdapter()
        with patch("bernstein.adapters.manager.process_alive", return_value=False):
            assert adapter.is_alive(1234) is False

    def test_process_alive_called_with_pid(self) -> None:
        adapter = ManagerAdapter()
        with patch("bernstein.adapters.manager.process_alive") as mock_alive:
            adapter.is_alive(5678)
        mock_alive.assert_called_once_with(5678)


# ---------------------------------------------------------------------------
# kill()
# ---------------------------------------------------------------------------


class TestKill:
    def test_returns_base_reap_receipt(self) -> None:
        adapter: CLIAdapter = ManagerAdapter()
        expected = ProcessReapReceipt(
            pgid=100,
            os_name="linux",
            method="posix_process_group",
            delivered=True,
            escalated=False,
            grace_seconds=3.0,
            confirmed_dead=True,
        )

        with patch("bernstein.adapters.base.reap_process_group", return_value=expected) as mock_reap:
            receipt = adapter.kill(100)

        assert receipt is expected
        assert receipt.pgid == 100
        mock_reap.assert_called_once_with(100)

    def test_undeliverable_stop_is_reported_not_raised(self) -> None:
        """A stop that cannot be delivered comes back as a receipt, not an exception.

        The previous ``ManagerAdapter.kill`` swallowed a failed
        ``kill_process_group`` and returned nothing.  The inherited reap path
        keeps the "must not raise" half of that behaviour and replaces the
        silent half: the failure is now stated on the receipt.
        """
        adapter: CLIAdapter = ManagerAdapter()

        with patch("bernstein.core.platform_compat.kill_process_group", return_value=False):
            receipt = adapter.kill(_UNUSED_PGID)

        assert receipt.pgid == _UNUSED_PGID
        assert receipt.delivered is False
        assert receipt.escalated is False


# ---------------------------------------------------------------------------
# name()
# ---------------------------------------------------------------------------


class TestName:
    def test_returns_internal_manager(self) -> None:
        assert ManagerAdapter().name() == "Internal Manager"
