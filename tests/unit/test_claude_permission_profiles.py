"""Tests for bernstein.core.claude_permission_profiles (CLAUDE-010)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.claude_permission_profiles import (
    PermissionProfile,
    PermissionProfileManager,
)


class TestPermissionProfile:
    def test_to_dict(self) -> None:
        p = PermissionProfile(
            role="qa",
            allowed_tools=("Bash", "Read"),
            deny_patterns=("*.env",),
        )
        d = p.to_dict()
        assert d["role"] == "qa"
        assert "Bash" in d["allowedTools"]
        assert "*.env" in d["denyPatterns"]

    def test_to_settings_json(self) -> None:
        p = PermissionProfile(
            role="qa",
            allowed_tools=("Bash", "Read"),
            disallowed_tools=("Write",),
        )
        s = p.to_settings_json()
        assert "allowedTools" in s
        assert "disallowedTools" in s

    def test_empty_profile(self) -> None:
        p = PermissionProfile(role="default")
        d = p.to_dict()
        assert "allowedTools" not in d
        assert "denyPatterns" not in d


class TestPermissionProfileManager:
    def test_get_builtin_profile(self) -> None:
        mgr = PermissionProfileManager()
        p = mgr.get_profile("qa")
        assert p.role == "qa"
        assert "Bash" in p.allowed_tools

    def test_get_unknown_role_returns_default(self) -> None:
        mgr = PermissionProfileManager()
        p = mgr.get_profile("custom_role")
        assert p.role == "custom_role"

    def test_override_takes_precedence(self) -> None:
        mgr = PermissionProfileManager()
        custom = PermissionProfile(
            role="qa",
            allowed_tools=("Read",),
            description="Custom QA profile",
        )
        mgr.set_override("qa", custom)
        p = mgr.get_profile("qa")
        assert p.allowed_tools == ("Read",)

    def test_clear_overrides(self) -> None:
        mgr = PermissionProfileManager()
        mgr.set_override("qa", PermissionProfile(role="qa", allowed_tools=("Read",)))
        mgr.clear_overrides()
        p = mgr.get_profile("qa")
        assert len(p.allowed_tools) > 1  # Back to builtin.

    def test_build_settings(self) -> None:
        mgr = PermissionProfileManager()
        settings = mgr.build_settings("qa")
        assert "allowedTools" in settings

    def test_inject_settings_creates_file(self, tmp_path: Path) -> None:
        mgr = PermissionProfileManager()
        path = mgr.inject_settings("qa", tmp_path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "allowedTools" in data

    def test_inject_settings_merges_existing(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        existing = {"customField": "value"}
        (settings_dir / "settings.json").write_text(json.dumps(existing))

        mgr = PermissionProfileManager()
        path = mgr.inject_settings("qa", tmp_path)
        data = json.loads(path.read_text())
        assert data["customField"] == "value"
        assert "allowedTools" in data

    def test_available_roles(self) -> None:
        mgr = PermissionProfileManager()
        roles = mgr.available_roles()
        assert "backend" in roles
        assert "qa" in roles
        assert "security" in roles

    def test_builtin_profiles_exist(self) -> None:
        mgr = PermissionProfileManager()
        for role in ("backend", "frontend", "qa", "security", "docs", "reviewer", "devops"):
            p = mgr.get_profile(role)
            assert p.role == role
