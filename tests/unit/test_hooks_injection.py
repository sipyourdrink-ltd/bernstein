"""Tests for Claude adapter hooks injection into .claude/settings.local.json."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.adapters.claude import ClaudeCodeAdapter

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _inject_hooks_config — settings.local.json generation
# ---------------------------------------------------------------------------


class TestInjectHooksConfig:
    """ClaudeCodeAdapter._inject_hooks_config() writes the hooks settings file."""

    def test_creates_settings_file(self, tmp_path: Path) -> None:
        """Settings file is created in .claude/ directory."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-abc")
        settings = tmp_path / ".claude" / "settings.local.json"
        assert settings.exists()

    def test_hooks_section_contains_all_events(self, tmp_path: Path) -> None:
        """All five hook events are configured."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-abc")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        expected_events = {"PostToolUse", "Stop", "PreCompact", "SubagentStart", "SubagentStop"}
        assert set(data["hooks"].keys()) == expected_events

    def test_hook_url_contains_session_id(self, tmp_path: Path) -> None:
        """Each hook URL includes the session ID for routing."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "my-session-42")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        for event_name, entries in data["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    assert "my-session-42" in hook["command"], (
                        f"Session ID missing from {event_name} hook command"
                    )

    def test_custom_server_url(self, tmp_path: Path) -> None:
        """Custom server URL is used in hook commands."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-1", server_url="http://10.0.0.1:9000")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        hook_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "http://10.0.0.1:9000/hooks/sess-1" in hook_cmd

    def test_hook_type_is_command(self, tmp_path: Path) -> None:
        """Each hook entry has type 'command'."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-type")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        for event_name, entries in data["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    assert hook["type"] == "command", f"{event_name} hook should be type 'command'"

    def test_merges_with_existing_settings(self, tmp_path: Path) -> None:
        """Existing settings are preserved when hooks are injected."""
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps({"permissions": {"allow": ["Read", "Write"]}}),
            encoding="utf-8",
        )

        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-merge")
        data = json.loads(settings_path.read_text(encoding="utf-8"))

        # Existing settings preserved
        assert data["permissions"]["allow"] == ["Read", "Write"]
        # Hooks added
        assert "hooks" in data
        assert "Stop" in data["hooks"]

    def test_overwrites_existing_hooks_section(self, tmp_path: Path) -> None:
        """Existing hooks section is replaced with fresh config."""
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps({"hooks": {"OldEvent": [{"hooks": [{"type": "command", "command": "old"}]}]}}),
            encoding="utf-8",
        )

        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-overwrite")
        data = json.loads(settings_path.read_text(encoding="utf-8"))

        # Old event is gone
        assert "OldEvent" not in data["hooks"]
        # New events present
        assert "Stop" in data["hooks"]

    def test_handles_corrupt_existing_settings(self, tmp_path: Path) -> None:
        """Corrupt settings file is overwritten gracefully."""
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.local.json").write_text("not valid json", encoding="utf-8")

        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-corrupt")

        data = json.loads((settings_dir / "settings.local.json").read_text(encoding="utf-8"))
        assert "hooks" in data

    def test_creates_claude_directory_if_missing(self, tmp_path: Path) -> None:
        """The .claude/ directory is created if it doesn't exist."""
        assert not (tmp_path / ".claude").exists()
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-mkdir")
        assert (tmp_path / ".claude" / "settings.local.json").exists()

    def test_hook_command_uses_curl(self, tmp_path: Path) -> None:
        """Hook command uses curl to POST stdin to the server."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-curl")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        hook_cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "curl" in hook_cmd
        assert "-d @-" in hook_cmd  # reads from stdin
        assert "Content-Type: application/json" in hook_cmd
