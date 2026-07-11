"""Tests for Claude adapter hooks injection into .claude/settings.local.json."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.adapters.claude import ClaudeCodeAdapter

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _inject_hooks_config - settings.local.json generation
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
                    assert "my-session-42" in hook["command"], f"Session ID missing from {event_name} hook command"

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
        """Hook command HMAC-signs body then POSTs via curl (audit-042)."""
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-curl")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        hook_cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "curl" in hook_cmd
        # Body is captured from stdin into a shell variable, then signed.
        assert "BODY=$(cat)" in hook_cmd
        assert '-d "$BODY"' in hook_cmd
        # HMAC-SHA256 signature via openssl + signature header.
        assert "openssl dgst -sha256 -hmac" in hook_cmd
        assert "X-Bernstein-Hook-Signature-256: sha256=$SIG" in hook_cmd
        assert "Content-Type: application/json" in hook_cmd


class TestEmbeddedAgentTeamsPin:
    """The settings.local.json env block pins the embedded agent-team gate off.

    Claude Code reads ``env`` from settings.local.json independently of the
    process env we filter in ``build_filtered_env``.  A spawned worker that
    inherits ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` can launch its own
    teammates, which run outside this worker's HMAC audit trail.  We pin the
    gate to ``"false"`` here (deny-by-default) unless the operator explicitly
    opts in.
    """

    def test_gate_pinned_false_by_default(self, tmp_path: Path, monkeypatch) -> None:
        """No opt-in -> env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS == 'false'."""
        monkeypatch.delenv("BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS", raising=False)
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-pin")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        assert data["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "false"

    def test_gate_pin_omitted_when_opted_in(self, tmp_path: Path, monkeypatch) -> None:
        """Opt-in -> the pin is not injected, leaving the gate untouched."""
        monkeypatch.setenv("BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS", "1")
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-optin")
        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text(encoding="utf-8"))

        env_block = data.get("env", {})
        assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in env_block

    def test_preexisting_true_gate_overwritten_keys_preserved(self, tmp_path: Path, monkeypatch) -> None:
        """Pre-existing gate 'true' -> overwritten to 'false'; other keys kept."""
        monkeypatch.delenv("BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS", raising=False)
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        settings_path = settings_dir / "settings.local.json"
        settings_path.write_text(
            json.dumps(
                {
                    "env": {
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "true",
                        "MY_UNRELATED_VAR": "keep-me",
                    }
                }
            ),
            encoding="utf-8",
        )

        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-overwrite-gate")
        data = json.loads(settings_path.read_text(encoding="utf-8"))

        assert data["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "false"
        assert data["env"]["MY_UNRELATED_VAR"] == "keep-me"


# ---------------------------------------------------------------------------
# In-process verification-gate hook merge (issue #2360)
# ---------------------------------------------------------------------------


class TestGateHookMerge:
    """A gate policy installed for a session adds blocking gate hooks."""

    def test_no_policy_leaves_events_unchanged(self, tmp_path: Path) -> None:
        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-nogate")
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        # No PreToolUse hook is injected absent a policy (degrade, AC4).
        assert "PreToolUse" not in data["hooks"]
        assert len(data["hooks"]["Stop"]) == 1

    def test_policy_present_injects_gate_hooks(self, tmp_path: Path) -> None:
        from bernstein.core.security.hook_gate import policy_from_task_fields, write_policy

        policy = policy_from_task_fields("sess-gate", owned_files=["src/**"], evidence_producers=[])
        write_policy(tmp_path, "sess-gate", policy)

        ClaudeCodeAdapter._inject_hooks_config(tmp_path, "sess-gate")
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(encoding="utf-8"))

        assert "PreToolUse" in data["hooks"]
        pre_cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "hook-gate" in pre_cmd
        assert "sess-gate" in pre_cmd
        # The Stop event now carries both the HTTP monitor hook and the gate.
        stop_cmds = [h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]]
        assert any("hook-gate" in c for c in stop_cmds)
