"""Unit tests for Claude Code per-task subagent injection (--agents flag)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.claude_agents import _SUBAGENTS, build_agents_json

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    """Return a minimal Popen mock with a usable stdout."""
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


@pytest.fixture(autouse=True)
def clean_adapter_state() -> Generator[None, None, None]:
    """Clear class-level proc dicts before and after every test."""
    ClaudeCodeAdapter._procs.clear()
    ClaudeCodeAdapter._wrapper_pids.clear()
    yield
    ClaudeCodeAdapter._procs.clear()
    ClaudeCodeAdapter._wrapper_pids.clear()


# ---------------------------------------------------------------------------
# build_agents_json() — unit tests
# ---------------------------------------------------------------------------


class TestBuildAgentsJson:
    """build_agents_json() generates correct subagent definitions per role."""

    def test_backend_role_has_qa_reviewer_and_explore(self) -> None:
        result = build_agents_json("backend")
        assert result is not None
        assert "qa-reviewer" in result
        assert "explore" in result

    def test_qa_role_has_explore(self) -> None:
        result = build_agents_json("qa")
        assert result is not None
        assert "explore" in result

    def test_security_role_has_explore(self) -> None:
        result = build_agents_json("security")
        assert result is not None
        assert "explore" in result

    def test_docs_role_has_explore(self) -> None:
        result = build_agents_json("docs")
        assert result is not None
        assert "explore" in result

    def test_unknown_role_returns_none(self) -> None:
        assert build_agents_json("unknown-role") is None

    def test_empty_role_returns_none(self) -> None:
        assert build_agents_json("") is None

    def test_all_agents_have_required_fields(self) -> None:
        """Every subagent definition must have description, prompt, and tools."""
        for role, agents in _SUBAGENTS.items():
            for name, defn in agents.items():
                assert "description" in defn, f"{role}/{name} missing description"
                assert "prompt" in defn, f"{role}/{name} missing prompt"
                assert "tools" in defn, f"{role}/{name} missing tools"
                assert isinstance(defn["tools"], list), f"{role}/{name} tools must be a list"
                assert len(defn["tools"]) > 0, f"{role}/{name} tools must be non-empty"

    def test_result_is_json_serializable(self) -> None:
        """The output must be valid JSON for --agents."""
        for role in _SUBAGENTS:
            result = build_agents_json(role)
            assert result is not None
            serialized = json.dumps(result)
            roundtrip = json.loads(serialized)
            assert roundtrip == result

    def test_result_is_a_copy_not_a_reference(self) -> None:
        """Callers must not mutate the module-level definitions."""
        result = build_agents_json("backend")
        assert result is not None
        result["qa-reviewer"]["description"] = "mutated"
        fresh = build_agents_json("backend")
        assert fresh is not None
        assert fresh["qa-reviewer"]["description"] != "mutated"

    def test_backend_qa_reviewer_has_bash_tool(self) -> None:
        """QA reviewer needs Bash to run tests."""
        result = build_agents_json("backend")
        assert result is not None
        assert "Bash" in result["qa-reviewer"]["tools"]

    def test_backend_explore_has_no_write_tools(self) -> None:
        """Explorer should be read-only."""
        result = build_agents_json("backend")
        assert result is not None
        tools = result["explore"]["tools"]
        for write_tool in ("Write", "Edit", "Bash"):
            assert write_tool not in tools


# ---------------------------------------------------------------------------
# _build_command() — --agents flag injection
# ---------------------------------------------------------------------------


class TestBuildCommandAgentsFlag:
    """_build_command() correctly injects --agents when agents_json is provided."""

    def test_agents_flag_included_when_agents_json_provided(self) -> None:
        adapter = ClaudeCodeAdapter()
        agents = {"explore": {"description": "test", "prompt": "test", "tools": ["Read"]}}
        cmd = adapter._build_command(
            ModelConfig(model="sonnet", effort="high"),
            None,
            "do something",
            agents_json=agents,
        )
        assert "--agents" in cmd
        idx = cmd.index("--agents")
        parsed = json.loads(cmd[idx + 1])
        assert parsed == agents

    def test_agents_flag_omitted_when_none(self) -> None:
        adapter = ClaudeCodeAdapter()
        cmd = adapter._build_command(
            ModelConfig(model="sonnet", effort="high"),
            None,
            "do something",
            agents_json=None,
        )
        assert "--agents" not in cmd

    def test_agents_flag_omitted_when_empty_dict(self) -> None:
        adapter = ClaudeCodeAdapter()
        cmd = adapter._build_command(
            ModelConfig(model="sonnet", effort="high"),
            None,
            "do something",
            agents_json={},
        )
        assert "--agents" not in cmd

    def test_agents_flag_before_prompt(self) -> None:
        """--agents must appear before -p (prompt is always last)."""
        adapter = ClaudeCodeAdapter()
        agents = {"explore": {"description": "test", "prompt": "test", "tools": ["Read"]}}
        cmd = adapter._build_command(
            ModelConfig(model="sonnet", effort="high"),
            None,
            "my prompt",
            agents_json=agents,
        )
        agents_idx = cmd.index("--agents")
        prompt_idx = cmd.index("-p")
        assert agents_idx < prompt_idx

    def test_agents_json_is_valid_json_string(self) -> None:
        """The --agents value must be a valid JSON string."""
        adapter = ClaudeCodeAdapter()
        agents = {
            "reviewer": {
                "description": "Reviews code",
                "prompt": "You are a reviewer",
                "tools": ["Read", "Grep"],
            }
        }
        cmd = adapter._build_command(
            ModelConfig(model="sonnet", effort="high"),
            None,
            "do something",
            agents_json=agents,
        )
        idx = cmd.index("--agents")
        parsed = json.loads(cmd[idx + 1])
        assert parsed["reviewer"]["tools"] == ["Read", "Grep"]


# ---------------------------------------------------------------------------
# spawn() — end-to-end: --agents injected for known roles
# ---------------------------------------------------------------------------


class TestSpawnAgentsInjection:
    """spawn() injects --agents for roles with subagent definitions."""

    def _spawn_and_get_cmd(
        self,
        tmp_path: Path,
        session_id: str,
    ) -> list[str]:
        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=5000)
        wrapper_mock = _make_popen_mock(pid=5001)

        with patch(
            "bernstein.adapters.claude.subprocess.Popen",
            side_effect=[claude_mock, wrapper_mock],
        ) as popen:
            adapter.spawn(
                prompt="do something",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id=session_id,
            )
            cmd: list[str] = popen.call_args_list[0].args[0]
        return cmd

    def test_backend_role_gets_agents_flag(self, tmp_path: Path) -> None:
        cmd = self._spawn_and_get_cmd(tmp_path, "backend-abc12345")
        assert "--agents" in cmd
        idx = cmd.index("--agents")
        parsed = json.loads(cmd[idx + 1])
        assert "qa-reviewer" in parsed
        assert "explore" in parsed

    def test_qa_role_gets_agents_flag(self, tmp_path: Path) -> None:
        cmd = self._spawn_and_get_cmd(tmp_path, "qa-abc12345")
        assert "--agents" in cmd
        idx = cmd.index("--agents")
        parsed = json.loads(cmd[idx + 1])
        assert "explore" in parsed

    def test_security_role_gets_agents_flag(self, tmp_path: Path) -> None:
        cmd = self._spawn_and_get_cmd(tmp_path, "security-abc12345")
        assert "--agents" in cmd

    def test_unknown_role_no_agents_flag(self, tmp_path: Path) -> None:
        cmd = self._spawn_and_get_cmd(tmp_path, "manager-abc12345")
        assert "--agents" not in cmd

    def test_reviewer_role_no_agents_flag(self, tmp_path: Path) -> None:
        """reviewer has allowed tools but no subagent definitions."""
        cmd = self._spawn_and_get_cmd(tmp_path, "reviewer-abc12345")
        assert "--agents" not in cmd
