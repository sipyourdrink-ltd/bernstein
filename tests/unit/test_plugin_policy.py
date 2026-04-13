"""Tests for the enterprise plugin allowlist/blocklist policy (T-devops)."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.plugin_policy import (
    PluginPolicy,
    PluginPolicyViolation,
    check_plugin_allowed,
    load_plugin_policy,
)

from bernstein.plugins import hookimpl
from bernstein.plugins.manager import PluginManager

# ---------------------------------------------------------------------------
# check_plugin_allowed unit tests
# ---------------------------------------------------------------------------


class TestCheckPluginAllowed:
    def test_empty_policy_allows_all(self) -> None:
        policy = PluginPolicy()
        # Must not raise for any name.
        check_plugin_allowed("anything", policy)
        check_plugin_allowed("dangerous", policy)

    def test_blocklist_rejects_plugin(self) -> None:
        policy = PluginPolicy(blocklist=frozenset({"bad-plugin"}))
        with pytest.raises(PluginPolicyViolation) as exc_info:
            check_plugin_allowed("bad-plugin", policy)
        assert "blocklist" in exc_info.value.reason

    def test_blocklist_overrides_allowlist(self) -> None:
        """A plugin in both blocklist and allowlist is still rejected."""
        policy = PluginPolicy(
            allowlist=frozenset({"bad-plugin"}),
            blocklist=frozenset({"bad-plugin"}),
        )
        with pytest.raises(PluginPolicyViolation):
            check_plugin_allowed("bad-plugin", policy)

    def test_blocklist_overrides_managed(self) -> None:
        """A plugin in both blocklist and managed list is still rejected."""
        policy = PluginPolicy(
            managed=frozenset({"bad-plugin"}),
            blocklist=frozenset({"bad-plugin"}),
        )
        with pytest.raises(PluginPolicyViolation):
            check_plugin_allowed("bad-plugin", policy)

    def test_allowlist_permits_listed_plugin(self) -> None:
        policy = PluginPolicy(allowlist=frozenset({"approved-plugin"}))
        check_plugin_allowed("approved-plugin", policy)  # must not raise

    def test_allowlist_rejects_unlisted_plugin(self) -> None:
        policy = PluginPolicy(allowlist=frozenset({"approved-plugin"}))
        with pytest.raises(PluginPolicyViolation) as exc_info:
            check_plugin_allowed("unknown-plugin", policy)
        assert "allowlist" in exc_info.value.reason

    def test_managed_bypasses_allowlist(self) -> None:
        """Managed plugins are allowed even when an allowlist is active."""
        policy = PluginPolicy(
            allowlist=frozenset({"approved-plugin"}),
            managed=frozenset({"audit-logger"}),
        )
        check_plugin_allowed("audit-logger", policy)  # must not raise

    def test_managed_only_allows_managed(self) -> None:
        """With only a managed list, non-managed plugins are allowed (no allowlist)."""
        policy = PluginPolicy(managed=frozenset({"audit-logger"}))
        check_plugin_allowed("any-other-plugin", policy)  # no allowlist = permit-all

    def test_violation_contains_plugin_name(self) -> None:
        policy = PluginPolicy(blocklist=frozenset({"evil"}))
        with pytest.raises(PluginPolicyViolation) as exc_info:
            check_plugin_allowed("evil", policy)
        assert exc_info.value.plugin_name == "evil"


# ---------------------------------------------------------------------------
# load_plugin_policy tests
# ---------------------------------------------------------------------------


class TestLoadPluginPolicy:
    def test_missing_file_returns_empty_policy(self, tmp_path: Path) -> None:
        policy = load_plugin_policy(tmp_path)
        assert policy.is_empty

    def test_loads_blocklist(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "blocklist:\n  - bad-plugin\n  - another-bad\n",
            encoding="utf-8",
        )
        policy = load_plugin_policy(tmp_path)
        assert "bad-plugin" in policy.blocklist
        assert "another-bad" in policy.blocklist
        assert not policy.allowlist

    def test_loads_allowlist(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "allowlist:\n  - approved\n",
            encoding="utf-8",
        )
        policy = load_plugin_policy(tmp_path)
        assert "approved" in policy.allowlist

    def test_loads_managed(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "managed:\n  - audit-logger\n",
            encoding="utf-8",
        )
        policy = load_plugin_policy(tmp_path)
        assert "audit-logger" in policy.managed

    def test_loads_combined_policy(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "allowlist:\n  - approved\nblocklist:\n  - blocked\nmanaged:\n  - audit-logger\n",
            encoding="utf-8",
        )
        policy = load_plugin_policy(tmp_path)
        assert "approved" in policy.allowlist
        assert "blocked" in policy.blocklist
        assert "audit-logger" in policy.managed

    def test_invalid_yaml_returns_empty_policy(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            ":\t[invalid\n",
            encoding="utf-8",
        )
        # Must not raise; returns empty policy.
        policy = load_plugin_policy(tmp_path)
        assert policy.is_empty

    def test_non_mapping_yaml_returns_empty_policy(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
        policy = load_plugin_policy(tmp_path)
        assert policy.is_empty

    def test_non_list_field_ignored(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        # blocklist is a string instead of a list.
        (policy_dir / "plugins-policy.yaml").write_text("blocklist: bad-plugin\n", encoding="utf-8")
        policy = load_plugin_policy(tmp_path)
        assert not policy.blocklist


# ---------------------------------------------------------------------------
# PluginManager integration tests
# ---------------------------------------------------------------------------


class _DummyPlugin:
    """Minimal plugin for testing policy enforcement in the manager."""

    def __init__(self) -> None:
        self.fired = False

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        self.fired = True


class TestPluginManagerPolicy:
    def test_blocked_plugin_rejected_at_load_time(self, tmp_path: Path) -> None:
        """A plugin on the blocklist must not be loadable via entry points."""
        # Write a policy that blocks "evil-plugin".
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "blocklist:\n  - evil-plugin\n",
            encoding="utf-8",
        )

        class _FakeEP:
            name = "evil-plugin"
            value = "fake.module:EvilPlugin"

            def load(self) -> type[_DummyPlugin]:
                return _DummyPlugin

        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        with patch("bernstein.plugins.manager.entry_points", return_value=[_FakeEP()]):
            pm.discover_entry_points()

        assert "evil-plugin" not in pm.registered_names

    def test_allowlist_rejects_unlisted_entry_point(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "allowlist:\n  - only-approved\n",
            encoding="utf-8",
        )

        class _FakeEP:
            name = "not-approved"
            value = "fake.module:Plugin"

            def load(self) -> type[_DummyPlugin]:
                return _DummyPlugin

        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        with patch("bernstein.plugins.manager.entry_points", return_value=[_FakeEP()]):
            pm.discover_entry_points()

        assert "not-approved" not in pm.registered_names

    def test_allowlist_permits_listed_entry_point(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "allowlist:\n  - good-plugin\n",
            encoding="utf-8",
        )

        class _FakeEP:
            name = "good-plugin"
            value = "fake.module:Plugin"

            def load(self) -> type[_DummyPlugin]:
                return _DummyPlugin

        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        with patch("bernstein.plugins.manager.entry_points", return_value=[_FakeEP()]):
            pm.discover_entry_points()

        assert "good-plugin" in pm.registered_names

    def test_managed_plugin_bypasses_allowlist(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "allowlist:\n  - approved\nmanaged:\n  - audit-logger\n",
            encoding="utf-8",
        )

        class _FakeEP:
            name = "audit-logger"
            value = "fake.module:AuditLogger"

            def load(self) -> type[_DummyPlugin]:
                return _DummyPlugin

        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        with patch("bernstein.plugins.manager.entry_points", return_value=[_FakeEP()]):
            pm.discover_entry_points()

        assert "audit-logger" in pm.registered_names

    def test_load_from_workdir_applies_policy(self, tmp_path: Path) -> None:
        """load_from_workdir() reads policy and blocks banned entry points."""
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "blocklist:\n  - banned\n",
            encoding="utf-8",
        )

        class _FakeEP:
            name = "banned"
            value = "fake.module:Plugin"

            def load(self) -> type[_DummyPlugin]:
                return _DummyPlugin

        pm = PluginManager(workdir=tmp_path)

        with patch("bernstein.plugins.manager.entry_points", return_value=[_FakeEP()]):
            pm.load_from_workdir(tmp_path)

        assert "banned" not in pm.registered_names

    def test_register_with_policy_enforcement_blocks_plugin(self, tmp_path: Path) -> None:
        """register(enforce_policy=True) raises PluginPolicyViolation for blocked plugins."""
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "blocklist:\n  - blocked\n",
            encoding="utf-8",
        )
        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        with pytest.raises(PluginPolicyViolation):
            pm.register(_DummyPlugin(), name="blocked", enforce_policy=True)

        assert "blocked" not in pm.registered_names

    def test_register_without_policy_enforcement_allows_plugin(self, tmp_path: Path) -> None:
        """register() without enforce_policy=True bypasses policy (backwards compat)."""
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "blocklist:\n  - blocked\n",
            encoding="utf-8",
        )
        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        # Default: no policy check — internal/test registrations are unaffected.
        pm.register(_DummyPlugin(), name="blocked")
        assert "blocked" in pm.registered_names

    def test_config_plugin_blocked_by_policy(self, tmp_path: Path) -> None:
        """Config-listed plugins are blocked when their name is in the blocklist."""
        policy_dir = tmp_path / ".bernstein"
        policy_dir.mkdir()
        (policy_dir / "plugins-policy.yaml").write_text(
            "blocklist:\n  - PluginManager\n",
            encoding="utf-8",
        )
        pm = PluginManager()
        pm._policy = load_plugin_policy(tmp_path)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            pm.discover_config_plugins(["bernstein.plugins.manager:PluginManager"])

        assert "bernstein.plugins.manager:PluginManager" not in pm.registered_names
