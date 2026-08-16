"""Tests for CatalogRegistry.load_configured_entries() (issue #3972).

Before this method existed, ``catalogs:`` entries parsed by
``seed_parser._parse_catalogs`` -> ``CatalogRegistry.from_config()`` only
ever reached ``discover()``'s ``_cached_roles`` metadata cache (a JSON file
under ``.sdd/agents/catalog.json``). ``match()`` reads only
``loaded_agents``, which nothing populated for configured entries - so a
configured catalog could never actually change a spawned prompt. These
tests assert the fix at the registry's ``match()`` boundary, one level
below the spawner-level assertion in ``test_spawner.py``.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from bernstein.agents.catalog import CatalogEntry, CatalogRegistry

if TYPE_CHECKING:
    from pathlib import Path

SKILL_MD = textwrap.dedent("""\
    ---
    name: qa-specialist
    description: Writes integration tests for the payments module.
    effort: high
    ---

    You are the QA specialist. Write integration tests before merging.
""")

AGENT_MD = textwrap.dedent("""\
    ---
    name: Payments Reviewer
    description: Reviews payments-module diffs for correctness.
    tools: [pytest]
    ---

    You are the Payments Reviewer. Focus on correctness and idempotency.
""")


def _generic_fixture(root: Path) -> Path:
    """A generic-type catalog directory: role-named subdir + SKILL.md."""
    skill_dir = root / "qa"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return root


def _plugin_fixture(root: Path) -> Path:
    """A plugin-layout catalog directory: standalone .claude/agents/*.md."""
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(AGENT_MD, encoding="utf-8")
    return root


class TestConfiguredCatalogsReachMatch:
    def test_a_configured_catalog_reaches_the_match_path_not_just_the_cache(self, tmp_path: Path) -> None:
        """The dead-path regression test (issue #3972, defect 2).

        A ``catalogs:`` entry pointing at a local fixture directory must
        produce a ``match()`` hit, not just a cache-file entry. Before the
        fix, ``load_configured_entries`` does not exist on
        ``CatalogRegistry`` at all.
        """
        catalog_root = _generic_fixture(tmp_path / "catalog")
        registry = CatalogRegistry.from_config([{"name": "local", "type": "generic", "path": str(catalog_root)}])

        loaded = registry.load_configured_entries()

        assert loaded == 1
        result = registry.match("qa", "write payments tests")
        assert result is not None
        assert result.name == "qa-specialist"
        assert "QA specialist" in result.system_prompt

    def test_plugin_type_catalog_reaches_the_match_path(self, tmp_path: Path) -> None:
        catalog_root = _plugin_fixture(tmp_path / "catalog")
        registry = CatalogRegistry.from_config([{"name": "local-plugins", "type": "plugin", "path": str(catalog_root)}])

        loaded = registry.load_configured_entries()

        assert loaded == 1
        # "Payments Reviewer" / "Reviews ... diffs" infers to role "reviewer".
        result = registry.match("reviewer", "review the payments diff")
        assert result is not None
        assert result.name == "Payments Reviewer"
        assert result.tools == ["pytest"]

    def test_default_registry_with_empty_config_is_unaffected(self) -> None:
        """Regression: the default (no `catalogs:` configured) path must be
        byte-for-byte unchanged - load_configured_entries() is a no-op on it."""
        registry = CatalogRegistry.default()

        loaded = registry.load_configured_entries()

        assert loaded == 0
        assert registry.loaded_agents == []

    def test_disabled_entry_is_not_loaded(self, tmp_path: Path) -> None:
        catalog_root = _generic_fixture(tmp_path / "catalog")
        entry = CatalogEntry(name="local", type="generic", enabled=False, path=str(catalog_root))
        registry = CatalogRegistry(entries=[entry])

        loaded = registry.load_configured_entries()

        assert loaded == 0
        assert registry.loaded_agents == []

    def test_agency_type_entries_are_not_touched(self, tmp_path: Path) -> None:
        """Scope boundary: this PR wires generic + plugin only (issue #3972).

        Agency-type entries keep whatever loading path they already have;
        widening that is out of scope here."""
        catalog_root = _generic_fixture(tmp_path / "catalog")
        entry = CatalogEntry(name="agency-local", type="agency", path=str(catalog_root))
        registry = CatalogRegistry(entries=[entry])

        loaded = registry.load_configured_entries()

        assert loaded == 0
        assert registry.loaded_agents == []

    def test_malformed_entry_is_logged_and_does_not_block_other_entries(self, tmp_path: Path, caplog) -> None:
        import logging

        missing_entry = CatalogEntry(name="missing", type="generic", path=str(tmp_path / "does-not-exist"))
        good_root = _generic_fixture(tmp_path / "catalog")
        good_entry = CatalogEntry(name="good", type="generic", path=str(good_root))
        registry = CatalogRegistry(entries=[missing_entry, good_entry])

        with caplog.at_level(logging.WARNING):
            loaded = registry.load_configured_entries()  # must not raise

        assert loaded == 1
        assert registry.match("qa", "anything") is not None
        assert "missing" in caplog.text

    def test_source_kind_directory_is_equivalent_to_legacy_path_only(self, tmp_path: Path) -> None:
        catalog_root = _generic_fixture(tmp_path / "catalog")
        legacy = CatalogRegistry(entries=[CatalogEntry(name="legacy", type="generic", path=str(catalog_root))])
        explicit = CatalogRegistry(
            entries=[CatalogEntry(name="explicit", type="generic", path=str(catalog_root), source_kind="directory")]
        )

        assert legacy.load_configured_entries() == explicit.load_configured_entries() == 1
        assert legacy.loaded_agents[0].name == explicit.loaded_agents[0].name

    def test_remote_source_kind_entry_is_skipped_with_warning_not_a_crash(self, tmp_path: Path, caplog) -> None:
        import logging

        remote_entry = CatalogEntry(name="remote", type="plugin", source="acme/agents", source_kind="github")
        good_root = _plugin_fixture(tmp_path / "catalog")
        good_entry = CatalogEntry(name="good", type="plugin", path=str(good_root))
        registry = CatalogRegistry(entries=[remote_entry, good_entry])

        with caplog.at_level(logging.WARNING):
            loaded = registry.load_configured_entries()  # must not raise

        assert loaded == 1
        assert registry.match("reviewer", "anything") is not None
