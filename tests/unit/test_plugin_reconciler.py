"""Tests for plugin reconciler — T2: auto-uninstall delisted plugins."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.plugin_reconciler import (
    MarketplaceEntry,
    ReconcileResult,
    get_installed_plugins,
    load_marketplace,
    reconcile_plugins,
)

# ---------------------------------------------------------------------------
# load_marketplace
# ---------------------------------------------------------------------------


class TestLoadMarketplace:
    def test_empty_if_no_file(self, tmp_path: Path) -> None:
        result = load_marketplace(tmp_path / "marketplace.yaml")
        assert result == []

    def test_loads_string_entries(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - audit-logger\n  - metrics\n", encoding="utf-8")
        entries = load_marketplace(mp)
        assert len(entries) == 2
        assert entries[0].name == "audit-logger"
        assert entries[1].name == "metrics"

    def test_loads_dict_entries_with_version(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text(
            "plugins:\n  - name: my-plugin\n    version: '1.2.0'\n",
            encoding="utf-8",
        )
        entries = load_marketplace(mp)
        assert len(entries) == 1
        assert entries[0].name == "my-plugin"
        assert entries[0].version == "1.2.0"

    def test_loads_mixed_entries(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text(
            "plugins:\n  - plain-name\n  - name: versioned\n    version: '2.0'\n",
            encoding="utf-8",
        )
        entries = load_marketplace(mp)
        assert len(entries) == 2
        names = {e.name for e in entries}
        assert "plain-name" in names
        assert "versioned" in names

    def test_empty_on_non_mapping(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text("- item1\n- item2\n", encoding="utf-8")
        assert load_marketplace(mp) == []

    def test_skips_blank_string_entries(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - ''\n  - valid-plugin\n", encoding="utf-8")
        entries = load_marketplace(mp)
        assert len(entries) == 1
        assert entries[0].name == "valid-plugin"

    def test_skips_dict_entries_without_name(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - version: '1.0'\n", encoding="utf-8")
        entries = load_marketplace(mp)
        assert entries == []

    def test_empty_plugins_list(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins: []\n", encoding="utf-8")
        assert load_marketplace(mp) == []


# ---------------------------------------------------------------------------
# get_installed_plugins
# ---------------------------------------------------------------------------


class TestGetInstalledPlugins:
    def test_empty_if_no_dir(self, tmp_path: Path) -> None:
        assert get_installed_plugins(tmp_path / "plugins") == []

    def test_lists_subdirectories(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        (plugins_dir / "plugin-a").mkdir(parents=True)
        (plugins_dir / "plugin-b").mkdir(parents=True)
        installed = get_installed_plugins(plugins_dir)
        assert installed == ["plugin-a", "plugin-b"]

    def test_files_are_ignored(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "some-plugin").mkdir()
        (plugins_dir / "readme.txt").write_text("x", encoding="utf-8")
        assert get_installed_plugins(plugins_dir) == ["some-plugin"]

    def test_sorted_alphabetically(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        for name in ["zebra", "alpha", "mango"]:
            (plugins_dir / name).mkdir(parents=True)
        assert get_installed_plugins(plugins_dir) == ["alpha", "mango", "zebra"]

    def test_empty_plugins_dir(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        assert get_installed_plugins(plugins_dir) == []


# ---------------------------------------------------------------------------
# reconcile_plugins
# ---------------------------------------------------------------------------


class TestReconcilePlugins:
    def test_no_marketplace_does_nothing(self, tmp_path: Path) -> None:
        """When marketplace file is absent, no plugins are removed."""
        plugins_dir = tmp_path / "plugins"
        (plugins_dir / "my-plugin").mkdir(parents=True)

        result = reconcile_plugins(plugins_dir, tmp_path / "marketplace.yaml")

        assert result.removed == []
        assert result.kept == []
        assert result.errors == []
        assert (plugins_dir / "my-plugin").exists()

    def test_keeps_listed_plugins(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        (plugins_dir / "keep-me").mkdir(parents=True)

        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - keep-me\n", encoding="utf-8")

        result = reconcile_plugins(plugins_dir, mp)

        assert result.kept == ["keep-me"]
        assert result.removed == []
        assert result.errors == []
        assert (plugins_dir / "keep-me").exists()

    def test_removes_delisted_plugin(self, tmp_path: Path) -> None:
        """Plugin not in marketplace is removed on startup."""
        plugins_dir = tmp_path / "plugins"
        (plugins_dir / "delisted-plugin").mkdir(parents=True)
        (plugins_dir / "keep-me").mkdir(parents=True)

        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - keep-me\n", encoding="utf-8")

        result = reconcile_plugins(plugins_dir, mp)

        assert "delisted-plugin" in result.removed
        assert "keep-me" in result.kept
        assert not (plugins_dir / "delisted-plugin").exists()
        assert (plugins_dir / "keep-me").exists()

    def test_removes_multiple_delisted_plugins(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        for name in ["a", "b", "c"]:
            (plugins_dir / name).mkdir(parents=True)

        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - a\n", encoding="utf-8")

        result = reconcile_plugins(plugins_dir, mp)

        assert sorted(result.removed) == ["b", "c"]
        assert result.kept == ["a"]
        assert (plugins_dir / "a").exists()
        assert not (plugins_dir / "b").exists()
        assert not (plugins_dir / "c").exists()

    def test_dry_run_does_not_remove(self, tmp_path: Path) -> None:
        """dry_run=True reports what would be removed without deleting."""
        plugins_dir = tmp_path / "plugins"
        (plugins_dir / "delisted").mkdir(parents=True)

        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - other\n", encoding="utf-8")

        result = reconcile_plugins(plugins_dir, mp, dry_run=True)

        assert "delisted" in result.removed
        # Directory must still exist — dry run only
        assert (plugins_dir / "delisted").exists()

    def test_no_plugins_dir_returns_empty(self, tmp_path: Path) -> None:
        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - something\n", encoding="utf-8")

        result = reconcile_plugins(tmp_path / "nonexistent-plugins", mp)

        assert result.removed == []
        assert result.kept == []
        assert result.errors == []

    def test_all_plugins_listed_none_removed(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        for name in ["alpha", "beta", "gamma"]:
            (plugins_dir / name).mkdir(parents=True)

        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - alpha\n  - beta\n  - gamma\n", encoding="utf-8")

        result = reconcile_plugins(plugins_dir, mp)

        assert result.removed == []
        assert sorted(result.kept) == ["alpha", "beta", "gamma"]

    def test_empty_plugins_dir_with_marketplace(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        mp = tmp_path / "marketplace.yaml"
        mp.write_text("plugins:\n  - something\n", encoding="utf-8")

        result = reconcile_plugins(plugins_dir, mp)

        assert result.removed == []
        assert result.kept == []

    def test_result_is_reconcile_result_type(self, tmp_path: Path) -> None:
        result = reconcile_plugins(tmp_path / "plugins", tmp_path / "marketplace.yaml")
        assert isinstance(result, ReconcileResult)

    def test_marketplace_entry_name_only(self, tmp_path: Path) -> None:
        """MarketplaceEntry with name only (no version) works correctly."""
        entry = MarketplaceEntry(name="test-plugin")
        assert entry.name == "test-plugin"
        assert entry.version == ""
