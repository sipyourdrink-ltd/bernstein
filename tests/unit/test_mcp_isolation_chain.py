"""What `--strict-mcp-config` does and does not change for a spawned claude agent (#5965).

The flag restricts Claude Code to the payload bernstein passes on `--mcp-config`,
so the CLI no longer merges in the servers it discovers itself (`claude mcp add`
scopes, a repo's `.mcp.json`, plugins, connectors). It does NOT keep
`~/.claude/mcp.json` out: the claude CLI never reads that file, bernstein's own
loader does, and puts its servers into the payload. These tests pin that chain
end to end, so the release note cannot claim more than the code delivers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import SpawnResult
from bernstein.adapters.claude_mcp_loader import load_mcp_config
from bernstein.adapters.plugin_sdk import AdapterCapability, AdapterPluginInfo, PluginAdapter

if TYPE_CHECKING:
    from pathlib import Path


class _RecordingAdapter(PluginAdapter):
    """Records the MCP payload the spawner hands the adapter, spawning nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_mcp_config: dict[str, Any] | None = None

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name="mcp-recording",
            version="1.0.0",
            capabilities=(AdapterCapability.SUPPORTS_SAMPLING_PARAMS,),
        )

    def health_check(self) -> bool:
        return True

    def supported_models(self) -> list[str]:
        return []

    def spawn(self, *, prompt: str, workdir: Path, mcp_config: dict[str, Any] | None = None, **_: Any) -> SpawnResult:
        self.seen_mcp_config = mcp_config
        return SpawnResult(pid=4242, log_path=workdir / "stub.log")

    def name(self) -> str:
        return "mcp-recording"


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global-srv": {"command": "global-mcp"}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_the_user_global_file_still_reaches_the_spawned_agent(tmp_path: Path, fake_home: Path, make_task) -> None:
    """The orchestrator's chain: loader, spawner, adapter payload.

    A task that declares no `mcp_servers` gets every server in `~/.claude/mcp.json`
    alongside the project's own. `--strict-mcp-config` cannot remove these, because
    they are the payload it restricts Claude Code to.
    """
    mcp_config = load_mcp_config({"project-srv": {"command": "project-mcp"}})
    adapter = _RecordingAdapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(
        adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        mcp_config=mcp_config,
        default_model="mock-model",
    )

    spawner.spawn_for_tasks([make_task(role="backend")])

    assert adapter.seen_mcp_config is not None
    servers = adapter.seen_mcp_config["mcpServers"]
    assert "global-srv" in servers, "bernstein's loader reads ~/.claude/mcp.json, not the claude CLI"
    assert "project-srv" in servers
