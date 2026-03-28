"""Tests for CatalogRegistry._fetch_from_providers, _load_entry, _load_generic_entry,
and _parse_catalog_entry (catalog.py coverage improvement)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from bernstein.agents.catalog import (
    CatalogEntry,
    CatalogRegistry,
    _parse_catalog_entry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    name: str = "test",
    type_: str = "agency",
    enabled: bool = True,
    priority: int = 50,
    path: str | None = None,
    source: str | None = None,
    glob: str | None = None,
    field_map: dict | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        name=name,
        type=type_,  # type: ignore[arg-type]
        enabled=enabled,
        priority=priority,
        path=path,
        source=source,
        glob=glob,
        field_map=field_map or {},
    )


# ---------------------------------------------------------------------------
# CatalogRegistry._fetch_from_providers (via discover)
# ---------------------------------------------------------------------------


class TestFetchFromProviders:
    def test_discover_with_local_agency_entry_populates_cached_roles(self, tmp_path):
        """discover() with a local agency entry calls _load_entry and populates _cached_roles."""
        cache_path = tmp_path / "catalog.json"
        entry = _make_entry(name="my-agency", type_="agency", path="/some/path")
        registry = CatalogRegistry(entries=[entry], _cache_path=cache_path)

        roles_from_provider = {
            "backend": {"description": "Backend engineer.", "model": "sonnet", "effort": "high"},
            "qa": {"description": "QA engineer.", "model": "sonnet", "effort": "normal"},
        }

        with patch.object(registry, "_load_entry", return_value=roles_from_provider):
            registry.discover()

        assert "backend" in registry._cached_roles
        assert "qa" in registry._cached_roles
        assert registry._cached_roles["backend"].source == "my-agency"
        assert registry._cached_roles["backend"].description == "Backend engineer."

    def test_discover_disabled_entry_is_skipped(self, tmp_path):
        """A disabled CatalogEntry should not be loaded."""
        cache_path = tmp_path / "catalog.json"
        entry = _make_entry(name="disabled-provider", type_="agency", enabled=False, path="/p")
        registry = CatalogRegistry(entries=[entry], _cache_path=cache_path)

        with patch.object(registry, "_load_entry", return_value={"backend": {}}) as mock_load:
            registry.discover()

        mock_load.assert_not_called()

    def test_discover_provider_failure_is_caught_and_logged(self, tmp_path, caplog):
        """A provider that raises should be caught and logged, not crash discover()."""
        import logging

        cache_path = tmp_path / "catalog.json"
        entry = _make_entry(name="broken-provider", type_="agency", path="/p")
        registry = CatalogRegistry(entries=[entry], _cache_path=cache_path)

        with patch.object(registry, "_load_entry", side_effect=RuntimeError("network error")):
            with caplog.at_level(logging.WARNING):
                registry.discover()  # must not raise

        assert "broken-provider" in caplog.text

    def test_discover_higher_priority_provider_wins_role_conflict(self, tmp_path):
        """When two providers expose the same role, the one already in _cached_roles wins."""
        cache_path = tmp_path / "catalog.json"
        entry_high = _make_entry(name="high", type_="agency", priority=100, path="/h")
        entry_low = _make_entry(name="low", type_="agency", priority=10, path="/l")
        # sorted descending by priority → high is processed first
        registry = CatalogRegistry(entries=[entry_high, entry_low], _cache_path=cache_path)

        def _fake_load(entry: CatalogEntry) -> dict:
            return {
                "backend": {
                    "description": f"from {entry.name}",
                    "model": "sonnet",
                    "effort": "normal",
                }
            }

        with patch.object(registry, "_load_entry", side_effect=_fake_load):
            registry.discover()

        # high-priority provider loaded first; low-priority must not overwrite
        assert registry._cached_roles["backend"].source == "high"

    def test_discover_uses_local_ttl_for_local_path_entry(self, tmp_path):
        """Entries with a local path get _LOCAL_TTL, remote entries get _REMOTE_TTL."""
        from bernstein.agents.catalog import _LOCAL_TTL

        cache_path = tmp_path / "catalog.json"
        entry = _make_entry(name="local", type_="agency", path="/some/local/path")
        registry = CatalogRegistry(entries=[entry], _cache_path=cache_path)

        with patch.object(registry, "_load_entry", return_value={"backend": {}}):
            registry.discover()

        assert registry._cached_roles["backend"].ttl_seconds == _LOCAL_TTL

    def test_discover_builtins_do_not_overwrite_provider_roles(self, tmp_path):
        """Built-in roles should not overwrite roles already fetched from providers."""
        cache_path = tmp_path / "catalog.json"
        entry = _make_entry(name="custom", type_="agency", path="/p")
        registry = CatalogRegistry(entries=[entry], _cache_path=cache_path)

        with patch.object(
            registry,
            "_load_entry",
            return_value={"backend": {"description": "Custom backend.", "model": "opus", "effort": "max"}},
        ):
            registry.discover()

        # The "backend" role was fetched from "custom", not from builtins
        assert registry._cached_roles["backend"].source == "custom"
        assert registry._cached_roles["backend"].model == "opus"


# ---------------------------------------------------------------------------
# CatalogRegistry._load_entry
# ---------------------------------------------------------------------------


class TestLoadEntry:
    def test_agency_local_path_calls_load_agency_catalog(self, tmp_path):
        """agency type with a local path calls load_agency_catalog."""
        registry = CatalogRegistry()
        entry = _make_entry(name="agency-local", type_="agency", path=str(tmp_path))

        mock_agent = MagicMock()
        mock_agent.role = "backend"
        mock_agent.description = "Backend engineer."

        with patch(
            "bernstein.core.agency_loader.load_agency_catalog",
            return_value={"backend-agent": mock_agent},
        ) as mock_loader:
            result = registry._load_entry(entry)

        mock_loader.assert_called_once()
        assert "backend" in result
        assert result["backend"]["description"] == "Backend engineer."

    def test_generic_local_path_calls_load_generic_entry(self, tmp_path):
        """generic type with a local path delegates to _load_generic_entry."""
        registry = CatalogRegistry()
        entry = _make_entry(name="generic-local", type_="generic", path=str(tmp_path))

        with patch.object(
            registry,
            "_load_generic_entry",
            return_value={"qa": {"description": "QA engineer."}},
        ) as mock_generic:
            result = registry._load_entry(entry)

        mock_generic.assert_called_once_with(entry)
        assert "qa" in result

    def test_remote_agency_no_path_returns_empty_dict(self):
        """Remote agency entry (no path) returns empty dict without loading."""
        registry = CatalogRegistry()
        entry = _make_entry(name="remote-agency", type_="agency", source="https://example.com")

        result = registry._load_entry(entry)

        assert result == {}

    def test_generic_without_path_returns_empty_dict(self):
        """generic type without a path returns empty dict."""
        registry = CatalogRegistry()
        entry = _make_entry(name="generic-remote", type_="generic", source="https://example.com")

        result = registry._load_entry(entry)

        assert result == {}


# ---------------------------------------------------------------------------
# CatalogRegistry._load_generic_entry
# ---------------------------------------------------------------------------


class TestLoadGenericEntry:
    def test_loads_yaml_files_with_default_fields(self, tmp_path):
        """Standard YAML files are loaded using default field names."""
        (tmp_path / "agent1.yaml").write_text(
            yaml.dump({"role": "backend", "description": "Backend engineer.", "model": "opus", "effort": "max"})
        )
        (tmp_path / "agent2.yaml").write_text(
            yaml.dump({"role": "qa", "description": "QA engineer.", "model": "sonnet", "effort": "normal"})
        )

        registry = CatalogRegistry()
        entry = _make_entry(name="generic", type_="generic", path=str(tmp_path))
        result = registry._load_generic_entry(entry)

        assert "backend" in result
        assert result["backend"]["description"] == "Backend engineer."
        assert result["backend"]["model"] == "opus"
        assert result["backend"]["effort"] == "max"
        assert "qa" in result

    def test_loads_yaml_with_custom_field_map(self, tmp_path):
        """field_map remaps YAML keys to canonical names."""
        (tmp_path / "agent.yaml").write_text(
            yaml.dump(
                {
                    "agent_role": "security",
                    "summary": "Security specialist.",
                    "llm_model": "sonnet",
                    "work_level": "high",
                }
            )
        )

        registry = CatalogRegistry()
        entry = _make_entry(
            name="generic",
            type_="generic",
            path=str(tmp_path),
            field_map={
                "role": "agent_role",
                "description": "summary",
                "model": "llm_model",
                "effort": "work_level",
            },
        )
        result = registry._load_generic_entry(entry)

        assert "security" in result
        assert result["security"]["description"] == "Security specialist."
        assert result["security"]["model"] == "sonnet"
        assert result["security"]["effort"] == "high"

    def test_skips_unreadable_file(self, tmp_path, caplog):
        """Unreadable files are logged as warnings and skipped."""
        import logging

        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("role: something")
        bad_file.chmod(0o000)  # make unreadable

        registry = CatalogRegistry()
        entry = _make_entry(name="generic", type_="generic", path=str(tmp_path))

        try:
            with caplog.at_level(logging.WARNING):
                result = registry._load_generic_entry(entry)
            # Should not raise; unreadable file skipped
            assert "something" not in result
        finally:
            bad_file.chmod(0o644)  # restore for cleanup

    def test_skips_non_dict_yaml(self, tmp_path):
        """YAML files that don't contain a mapping at the top level are skipped."""
        (tmp_path / "list.yaml").write_text(yaml.dump(["item1", "item2"]))
        (tmp_path / "scalar.yaml").write_text(yaml.dump("just a string"))
        (tmp_path / "valid.yaml").write_text(yaml.dump({"role": "devops", "description": "DevOps."}))

        registry = CatalogRegistry()
        entry = _make_entry(name="generic", type_="generic", path=str(tmp_path))
        result = registry._load_generic_entry(entry)

        assert "devops" in result
        assert len(result) == 1  # list and scalar skipped

    def test_skips_yaml_without_role_field(self, tmp_path):
        """YAML files without a role field (or mapped role field) are skipped."""
        (tmp_path / "no_role.yaml").write_text(yaml.dump({"description": "No role here.", "model": "sonnet"}))

        registry = CatalogRegistry()
        entry = _make_entry(name="generic", type_="generic", path=str(tmp_path))
        result = registry._load_generic_entry(entry)

        assert result == {}

    def test_custom_glob_pattern(self, tmp_path):
        """Only files matching the glob pattern are loaded."""
        (tmp_path / "agent.yaml").write_text(yaml.dump({"role": "backend", "description": "Backend."}))
        (tmp_path / "agent.yml").write_text(yaml.dump({"role": "qa", "description": "QA."}))

        registry = CatalogRegistry()
        entry = _make_entry(name="generic", type_="generic", path=str(tmp_path), glob="*.yml")
        result = registry._load_generic_entry(entry)

        # Only .yml file matches
        assert "qa" in result
        assert "backend" not in result

    def test_returns_empty_dict_for_empty_directory(self, tmp_path):
        """Empty directory with no matching files returns empty dict."""
        registry = CatalogRegistry()
        entry = _make_entry(name="generic", type_="generic", path=str(tmp_path))
        result = registry._load_generic_entry(entry)
        assert result == {}


# ---------------------------------------------------------------------------
# _parse_catalog_entry
# ---------------------------------------------------------------------------


class TestParseCatalogEntry:
    def test_valid_agency_entry(self):
        raw = {
            "name": "agency-default",
            "type": "agency",
            "enabled": True,
            "priority": 100,
            "source": "https://github.com/example/agents",
        }
        entry = _parse_catalog_entry(raw)
        assert entry.name == "agency-default"
        assert entry.type == "agency"
        assert entry.enabled is True
        assert entry.priority == 100
        assert entry.source == "https://github.com/example/agents"

    def test_valid_generic_entry_with_field_map(self):
        raw = {
            "name": "my-generic",
            "type": "generic",
            "path": "/some/path",
            "glob": "**/*.yaml",
            "field_map": {"role": "agent_role", "description": "summary"},
        }
        entry = _parse_catalog_entry(raw)
        assert entry.name == "my-generic"
        assert entry.type == "generic"
        assert entry.path == "/some/path"
        assert entry.glob == "**/*.yaml"
        assert entry.field_map == {"role": "agent_role", "description": "summary"}

    def test_defaults_applied_when_optional_fields_missing(self):
        raw = {"name": "minimal", "type": "agency"}
        entry = _parse_catalog_entry(raw)
        assert entry.enabled is True
        assert entry.priority == 50
        assert entry.source is None
        assert entry.path is None
        assert entry.field_map == {}

    def test_missing_name_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required string 'name'"):
            _parse_catalog_entry({"type": "agency"})

    def test_empty_name_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required string 'name'"):
            _parse_catalog_entry({"name": "", "type": "agency"})

    def test_non_string_name_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required string 'name'"):
            _parse_catalog_entry({"name": 42, "type": "agency"})

    def test_invalid_type_raises_value_error(self):
        with pytest.raises(ValueError, match="type must be 'agency' or 'generic'"):
            _parse_catalog_entry({"name": "test", "type": "invalid"})

    def test_missing_type_raises_value_error(self):
        with pytest.raises(ValueError, match="type must be 'agency' or 'generic'"):
            _parse_catalog_entry({"name": "test"})

    def test_non_bool_enabled_raises_value_error(self):
        with pytest.raises(ValueError, match="enabled must be a bool"):
            _parse_catalog_entry({"name": "test", "type": "agency", "enabled": "yes"})

    def test_non_int_priority_raises_value_error(self):
        with pytest.raises(ValueError, match="priority must be an int"):
            _parse_catalog_entry({"name": "test", "type": "agency", "priority": "high"})

    def test_non_string_source_raises_value_error(self):
        with pytest.raises(ValueError, match="source must be a string"):
            _parse_catalog_entry({"name": "test", "type": "agency", "source": 123})

    def test_non_string_path_raises_value_error(self):
        with pytest.raises(ValueError, match="path must be a string"):
            _parse_catalog_entry({"name": "test", "type": "generic", "path": 999})

    def test_non_string_format_raises_value_error(self):
        with pytest.raises(ValueError, match="format must be a string"):
            _parse_catalog_entry({"name": "test", "type": "generic", "format": ["yaml"]})

    def test_non_string_glob_raises_value_error(self):
        with pytest.raises(ValueError, match="glob must be a string"):
            _parse_catalog_entry({"name": "test", "type": "generic", "glob": True})

    def test_non_dict_field_map_raises_value_error(self):
        with pytest.raises(ValueError, match="field_map must be a string-to-string mapping"):
            _parse_catalog_entry({"name": "test", "type": "generic", "field_map": "not-a-dict"})

    def test_field_map_with_non_string_values_raises_value_error(self):
        with pytest.raises(ValueError, match="field_map must be a string-to-string mapping"):
            _parse_catalog_entry({"name": "test", "type": "generic", "field_map": {"role": 42}})

    def test_disabled_entry_parsed_correctly(self):
        raw = {"name": "disabled", "type": "agency", "enabled": False}
        entry = _parse_catalog_entry(raw)
        assert entry.enabled is False

    def test_from_config_skips_disabled_entries(self):
        """CatalogRegistry.from_config should skip disabled catalog entries."""
        configs = [
            {"name": "active", "type": "agency", "enabled": True, "priority": 50},
            {"name": "inactive", "type": "agency", "enabled": False, "priority": 100},
        ]
        registry = CatalogRegistry.from_config(configs)
        names = [e.name for e in registry.entries]
        assert "active" in names
        assert "inactive" not in names

    def test_from_config_sorts_by_priority_descending(self):
        """from_config sorts entries by priority, highest first."""
        configs = [
            {"name": "low", "type": "agency", "priority": 10},
            {"name": "high", "type": "agency", "priority": 200},
            {"name": "mid", "type": "agency", "priority": 50},
        ]
        registry = CatalogRegistry.from_config(configs)
        priorities = [e.priority for e in registry.entries]
        assert priorities == sorted(priorities, reverse=True)
