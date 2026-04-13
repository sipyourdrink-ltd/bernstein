"""Tests for Neovim integration bridge."""

from __future__ import annotations

import pytest

from bernstein.core.protocols.neovim_bridge import (
    NeovimBridge,
    NeovimCommand,
    NeovimEvent,
    generate_plugin_lua,
    render_setup_guide,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bridge() -> NeovimBridge:
    return NeovimBridge(server_url="http://127.0.0.1:9999")


# ---------------------------------------------------------------------------
# Tests -- NeovimCommand dataclass
# ---------------------------------------------------------------------------


class TestNeovimCommand:
    def test_frozen(self) -> None:
        cmd = NeovimCommand(name="Run", description="Start", args_spec="")
        with pytest.raises(AttributeError):
            cmd.name = "Other"  # type: ignore[misc]

    def test_fields(self) -> None:
        cmd = NeovimCommand(name="BernsteinRun", description="Start a run", args_spec="[file]")
        assert cmd.name == "BernsteinRun"
        assert cmd.description == "Start a run"
        assert cmd.args_spec == "[file]"

    def test_equality(self) -> None:
        a = NeovimCommand(name="X", description="Y", args_spec="Z")
        b = NeovimCommand(name="X", description="Y", args_spec="Z")
        assert a == b


# ---------------------------------------------------------------------------
# Tests -- NeovimEvent dataclass
# ---------------------------------------------------------------------------


class TestNeovimEvent:
    def test_frozen(self) -> None:
        evt = NeovimEvent(event_type="cmd", data={})
        with pytest.raises(AttributeError):
            evt.event_type = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        evt = NeovimEvent(event_type="test")
        assert evt.data == {}
        assert evt.timestamp > 0

    def test_custom_data(self) -> None:
        evt = NeovimEvent(event_type="cmd", data={"key": "val"}, timestamp=100.0)
        assert evt.data["key"] == "val"
        assert evt.timestamp == 100.0


# ---------------------------------------------------------------------------
# Tests -- NeovimBridge.get_available_commands
# ---------------------------------------------------------------------------


class TestGetAvailableCommands:
    def test_returns_list(self, bridge: NeovimBridge) -> None:
        cmds = bridge.get_available_commands()
        assert isinstance(cmds, list)
        assert len(cmds) == 4

    def test_all_neovim_commands(self, bridge: NeovimBridge) -> None:
        names = {c.name for c in bridge.get_available_commands()}
        assert names == {"BernsteinRun", "BernsteinStatus", "BernsteinPlan", "BernsteinStop"}

    def test_command_types(self, bridge: NeovimBridge) -> None:
        for cmd in bridge.get_available_commands():
            assert isinstance(cmd, NeovimCommand)
            assert cmd.description != ""


# ---------------------------------------------------------------------------
# Tests -- NeovimBridge.handle_command
# ---------------------------------------------------------------------------


class TestHandleCommand:
    def test_unknown_command(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("Nonexistent", {})
        assert resp["ok"] is False
        assert "Unknown command" in resp["message"]

    def test_run_no_args(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinRun", {})
        assert resp["ok"] is True
        assert "Run started" in resp["message"]

    def test_run_with_plan(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinRun", {"plan_file": "plan.yaml"})
        assert resp["ok"] is True
        assert "plan.yaml" in resp["message"]
        assert resp["server_url"] == "http://127.0.0.1:9999"

    def test_status(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinStatus", {})
        assert resp["ok"] is True
        assert resp["server_url"] == "http://127.0.0.1:9999"

    def test_plan_with_file(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinPlan", {"plan_file": "my.yaml"})
        assert resp["ok"] is True
        assert "my.yaml" in resp["message"]

    def test_plan_no_file(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinPlan", {})
        assert resp["ok"] is True
        assert "No plan file" in resp["message"]

    def test_stop(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinStop", {})
        assert resp["ok"] is True
        assert "Stop" in resp["message"]

    def test_none_args(self, bridge: NeovimBridge) -> None:
        resp = bridge.handle_command("BernsteinRun", None)
        assert resp["ok"] is True


# ---------------------------------------------------------------------------
# Tests -- NeovimBridge.format_status_line
# ---------------------------------------------------------------------------


class TestFormatStatusLine:
    def test_basic(self, bridge: NeovimBridge) -> None:
        line = bridge.format_status_line(
            {"total_tasks": 10, "completed_tasks": 3, "active_agents": 2, "state": "running"}
        )
        assert line == "[BST:running] 3/10 tasks | 2 agents"

    def test_defaults(self, bridge: NeovimBridge) -> None:
        line = bridge.format_status_line({})
        assert line == "[BST:unknown] 0/0 tasks | 0 agents"

    def test_partial(self, bridge: NeovimBridge) -> None:
        line = bridge.format_status_line({"total_tasks": 5, "state": "idle"})
        assert "[BST:idle]" in line
        assert "0/5" in line


# ---------------------------------------------------------------------------
# Tests -- NeovimBridge.format_split_output
# ---------------------------------------------------------------------------


class TestFormatSplitOutput:
    def test_basic(self, bridge: NeovimBridge) -> None:
        lines = bridge.format_split_output("hello\nworld")
        assert lines[0] == "--- Bernstein Agent Output ---"
        assert lines[-1] == "--- End ---"
        assert "hello" in lines
        assert "world" in lines

    def test_wraps_long_lines(self, bridge: NeovimBridge) -> None:
        long = "x" * 200
        lines = bridge.format_split_output(long)
        # Should be wrapped — no single line > 120 chars
        for line in lines:
            assert len(line) <= 120

    def test_empty_output(self, bridge: NeovimBridge) -> None:
        lines = bridge.format_split_output("")
        assert lines[0] == "--- Bernstein Agent Output ---"
        assert lines[-1] == "--- End ---"


# ---------------------------------------------------------------------------
# Tests -- NeovimBridge.get_diff_annotations
# ---------------------------------------------------------------------------


class TestGetDiffAnnotations:
    def test_added(self, bridge: NeovimBridge) -> None:
        anns = bridge.get_diff_annotations(
            [{"file": "a.py", "line": 10, "change_type": "added", "summary": "new func"}]
        )
        assert len(anns) == 1
        assert anns[0]["sign"] == "+"
        assert "[BST]" in anns[0]["text"]

    def test_modified(self, bridge: NeovimBridge) -> None:
        anns = bridge.get_diff_annotations([{"file": "b.py", "line": 5, "change_type": "modified"}])
        assert anns[0]["sign"] == "~"

    def test_deleted(self, bridge: NeovimBridge) -> None:
        anns = bridge.get_diff_annotations([{"file": "c.py", "line": 1, "change_type": "deleted"}])
        assert anns[0]["sign"] == "-"

    def test_unknown_type(self, bridge: NeovimBridge) -> None:
        anns = bridge.get_diff_annotations([{"file": "d.py", "line": 1, "change_type": "renamed"}])
        assert anns[0]["sign"] == "?"

    def test_empty_list(self, bridge: NeovimBridge) -> None:
        assert bridge.get_diff_annotations([]) == []

    def test_multiple(self, bridge: NeovimBridge) -> None:
        anns = bridge.get_diff_annotations(
            [
                {"file": "a.py", "line": 1, "change_type": "added"},
                {"file": "b.py", "line": 2, "change_type": "deleted"},
            ]
        )
        assert len(anns) == 2


# ---------------------------------------------------------------------------
# Tests -- generate_plugin_lua
# ---------------------------------------------------------------------------


class TestGeneratePluginLua:
    def test_contains_header(self) -> None:
        lua = generate_plugin_lua()
        assert "-- Bernstein Neovim integration" in lua

    def test_contains_commands(self) -> None:
        lua = generate_plugin_lua()
        assert "BernsteinRun" in lua
        assert "BernsteinStatus" in lua
        assert "BernsteinPlan" in lua
        assert "BernsteinStop" in lua

    def test_custom_url(self) -> None:
        lua = generate_plugin_lua(server_url="http://localhost:1234")
        assert "http://localhost:1234" in lua

    def test_default_url(self) -> None:
        lua = generate_plugin_lua()
        assert "http://127.0.0.1:8052" in lua

    def test_returns_string(self) -> None:
        assert isinstance(generate_plugin_lua(), str)

    def test_contains_setup(self) -> None:
        lua = generate_plugin_lua()
        assert "function M.setup" in lua
        assert "return M" in lua


# ---------------------------------------------------------------------------
# Tests -- render_setup_guide
# ---------------------------------------------------------------------------


class TestRenderSetupGuide:
    def test_contains_title(self) -> None:
        guide = render_setup_guide()
        assert "# Bernstein Neovim Plugin" in guide

    def test_contains_commands(self) -> None:
        guide = render_setup_guide()
        assert "BernsteinRun" in guide
        assert "BernsteinStop" in guide

    def test_contains_install(self) -> None:
        guide = render_setup_guide()
        assert "lazy.nvim" in guide
        assert 'require("bernstein")' in guide

    def test_returns_string(self) -> None:
        assert isinstance(render_setup_guide(), str)
