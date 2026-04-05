"""Unit tests for IaCAdapter (Terraform/Pulumi)."""

from __future__ import annotations

import signal
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.iac import IaCAdapter, _build_iac_script, _detect_tool
from bernstein.core.models import ModelConfig

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestIaCAdapterInit:
    def test_default_no_tool(self) -> None:
        adapter = IaCAdapter()
        assert adapter._tool is None

    def test_explicit_terraform(self) -> None:
        adapter = IaCAdapter(tool="terraform")
        assert adapter._tool == "terraform"

    def test_explicit_pulumi(self) -> None:
        adapter = IaCAdapter(tool="pulumi")
        assert adapter._tool == "pulumi"

    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown IaC tool"):
            IaCAdapter(tool="cloudformation")


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIaCIsAvailable:
    def test_true_when_terraform_present(self) -> None:
        adapter = IaCAdapter()
        with patch("bernstein.adapters.iac.shutil.which", side_effect=lambda x: "/usr/bin/terraform" if x == "terraform" else None):
            assert adapter.is_available() is True

    def test_true_when_pulumi_present(self) -> None:
        adapter = IaCAdapter()
        with patch("bernstein.adapters.iac.shutil.which", side_effect=lambda x: "/usr/bin/pulumi" if x == "pulumi" else None):
            assert adapter.is_available() is True

    def test_false_when_neither_present(self) -> None:
        adapter = IaCAdapter()
        with patch("bernstein.adapters.iac.shutil.which", return_value=None):
            assert adapter.is_available() is False


# ---------------------------------------------------------------------------
# _detect_tool()
# ---------------------------------------------------------------------------


class TestDetectTool:
    def test_prefers_terraform(self) -> None:
        with patch("bernstein.adapters.iac.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            assert _detect_tool() == "terraform"

    def test_falls_back_to_pulumi(self) -> None:
        def which_mock(name: str) -> str | None:
            return "/usr/bin/pulumi" if name == "pulumi" else None

        with patch("bernstein.adapters.iac.shutil.which", side_effect=which_mock):
            assert _detect_tool() == "pulumi"

    def test_returns_none(self) -> None:
        with patch("bernstein.adapters.iac.shutil.which", return_value=None):
            assert _detect_tool() is None


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------


class TestIaCAdapterSpawn:
    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="terraform")
        proc_mock = _make_popen_mock(pid=700)
        with patch("bernstein.adapters.iac.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="deploy vpc",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "bash"

    def test_spawn_writes_script(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="terraform")
        proc_mock = _make_popen_mock(pid=701)
        with patch("bernstein.adapters.iac.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="deploy vpc",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-s2",
            )
        script = tmp_path / ".sdd" / "runtime" / "iac-s2-iac.sh"
        assert script.exists()
        content = script.read_text()
        assert "terraform plan" in content
        assert "terraform apply" in content

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="terraform")
        proc_mock = _make_popen_mock(pid=702)
        with patch("bernstein.adapters.iac.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="deploy",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-s3",
            )
        assert result.pid == 702

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="terraform")
        proc_mock = _make_popen_mock(pid=703)
        with patch("bernstein.adapters.iac.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="deploy",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="my-iac-session",
            )
        assert result.log_path.name == "my-iac-session.log"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="pulumi")
        proc_mock = _make_popen_mock(pid=704)
        with patch("bernstein.adapters.iac.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="deploy",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-s4",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_pulumi_script_content(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="pulumi")
        proc_mock = _make_popen_mock(pid=705)
        with patch("bernstein.adapters.iac.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="deploy stack",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-s5",
            )
        script = tmp_path / ".sdd" / "runtime" / "iac-s5-iac.sh"
        content = script.read_text()
        assert "pulumi preview" in content
        assert "pulumi up" in content

    def test_no_tool_raises_without_detection(self, tmp_path: Path) -> None:
        adapter = IaCAdapter()
        with (
            patch("bernstein.adapters.iac.shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="No IaC tool found"),
        ):
            adapter.spawn(
                prompt="deploy",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-s6",
            )

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="terraform")
        with (
            patch(
                "bernstein.adapters.iac.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="deploy",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-err1",
            )

    def test_permission_error_raises(self, tmp_path: Path) -> None:
        adapter = IaCAdapter(tool="terraform")
        with (
            patch(
                "bernstein.adapters.iac.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="deploy",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="iac-err2",
            )


# ---------------------------------------------------------------------------
# Plan-before-apply safety
# ---------------------------------------------------------------------------


class TestPlanBeforeApply:
    def test_terraform_script_has_plan_before_apply(self) -> None:
        script = _build_iac_script("terraform", "deploy vpc")
        plan_pos = script.index("terraform plan")
        apply_pos = script.index("terraform apply")
        assert plan_pos < apply_pos

    def test_terraform_script_checks_exit_code(self) -> None:
        script = _build_iac_script("terraform", "deploy")
        assert "plan_exit=$?" in script
        assert "exit 1" in script

    def test_pulumi_script_has_preview_before_up(self) -> None:
        script = _build_iac_script("pulumi", "deploy stack")
        preview_pos = script.index("pulumi preview")
        up_pos = script.index("pulumi up")
        assert preview_pos < up_pos

    def test_pulumi_script_checks_exit_code(self) -> None:
        script = _build_iac_script("pulumi", "deploy")
        assert "$? -ne 0" in script

    def test_terraform_script_uses_detailed_exitcode(self) -> None:
        script = _build_iac_script("terraform", "deploy")
        assert "-detailed-exitcode" in script

    def test_terraform_script_auto_approve(self) -> None:
        script = _build_iac_script("terraform", "deploy")
        assert "-auto-approve" in script


# ---------------------------------------------------------------------------
# name()
# ---------------------------------------------------------------------------


class TestIaCAdapterName:
    def test_name(self) -> None:
        assert IaCAdapter().name() == "IaC (Terraform/Pulumi)"


# ---------------------------------------------------------------------------
# is_alive() and kill() — inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestIaCIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = IaCAdapter()
        with patch("bernstein.adapters.base.os.kill", return_value=None):
            assert adapter.is_alive(1234) is True

    def test_false_when_oserror(self) -> None:
        adapter = IaCAdapter()
        with patch("bernstein.adapters.base.os.kill", side_effect=OSError("no such process")):
            assert adapter.is_alive(9999) is False


class TestIaCKill:
    def test_calls_killpg_with_pid_as_pgid(self) -> None:
        adapter = IaCAdapter()
        with patch("bernstein.adapters.base.os.killpg") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555, signal.SIGTERM)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestIaCRegistry:
    def test_iac_in_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("iac")
        assert isinstance(adapter, IaCAdapter)

    def test_iac_name_via_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        assert get_adapter("iac").name() == "IaC (Terraform/Pulumi)"
