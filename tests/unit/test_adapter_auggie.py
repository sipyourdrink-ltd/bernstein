"""Unit tests for AuggieAdapter spawn command construction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.auggie import AuggieAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


class TestAuggieAdapterSpawn:
    """AuggieAdapter.spawn() builds correct command."""

    def test_spawn_builds_run_command(self, tmp_path: Path) -> None:
        adapter = AuggieAdapter()
        proc_mock = make_popen_mock(pid=700)
        with patch("bernstein.adapters.auggie.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="auggie-s1",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[:3] == ["auggie", "--print", "--allow-indexing"]
        assert inner[-1] == "fix the bug"

    def test_spawn_runs_in_print_mode(self, tmp_path: Path) -> None:
        """A spawned agent has no TTY. Without --print, auggie 0.36 announces a
        switch to print mode and then never exits, so an agent that could not
        even authenticate held its slot until the watchdog."""
        adapter = AuggieAdapter()
        proc_mock = make_popen_mock(pid=701)
        with patch("bernstein.adapters.auggie.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="auggie-s2",
            )
        assert "--print" in inner_cmd(popen.call_args.args[0])

    def test_spawn_forwards_the_auth_variables_auggie_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUGMENT_SESSION_AUTH", '{"accessToken": "t", "tenantURL": "https://x"}')
        monkeypatch.setenv("AUGMENT_API_TOKEN", "token")
        monkeypatch.setenv("AUGMENT_API_URL", "https://tenant.example")
        monkeypatch.setenv("AUGMENT_API_KEY", "never-read-by-auggie")
        adapter = AuggieAdapter()
        proc_mock = make_popen_mock(pid=702)
        with patch("bernstein.adapters.auggie.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="auggie-s3",
            )
        env = popen.call_args.kwargs["env"]
        assert env["AUGMENT_SESSION_AUTH"].startswith("{")
        assert env["AUGMENT_API_TOKEN"] == "token"
        assert env["AUGMENT_API_URL"] == "https://tenant.example"
        assert "AUGMENT_API_KEY" not in env

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = AuggieAdapter()
        with (
            patch(
                "bernstein.adapters.auggie.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="auggie-missing",
            )
        msg = str(exc_info.value)
        assert "auggie not found" in msg
        assert "npm install -g @augmentcode/auggie" in msg


class TestAuggieAdapterName:
    def test_name(self) -> None:
        assert AuggieAdapter().name() == "Auggie"
