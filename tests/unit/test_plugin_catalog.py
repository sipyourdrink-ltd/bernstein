"""Tests for the Claude Code plugin/subagent layout reader (issue #3972).

Covers:
- ``parse_agent_file``: frontmatter + body parsing, malformed input as a
  named ``AgentDefinitionError`` (never a silent skip).
- ``load_plugin_catalog``: discovery across the three on-disk shapes -
  standalone ``.claude/agents/*.md``, ``plugins/<name>/agents/*.md``, and
  the ``.claude-plugin/marketplace.json`` index that scopes which plugins
  are read.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from bernstein.agents.catalog import CatalogAgent
from bernstein.agents.plugin_catalog import (
    AgentDefinitionError,
    load_plugin_catalog,
    parse_agent_file,
)

if TYPE_CHECKING:
    from pathlib import Path

FULL_AGENT_MD = textwrap.dedent("""\
    ---
    name: Security Reviewer
    description: Audits pull requests for security vulnerabilities.
    model: opus
    tools: [ruff, mypy, semgrep]
    ---

    # Security Reviewer

    You are the Security Reviewer agent. Audit every diff for injection,
    auth bypass, and secret leakage before it merges.
""")

MINIMAL_AGENT_MD = textwrap.dedent("""\
    ---
    name: Minimal Agent
    description: Bare-bones agent with no optional fields.
    ---

    Just the basics.
""")

NO_FENCE_MD = "# No frontmatter here\n\nJust a body."

UNTERMINATED_FENCE_MD = "---\nname: Broken\ndescription: no closing fence\nbody text without a second fence\n"

INVALID_YAML_MD = "---\nname: [unclosed\ndescription: bad yaml\n---\nBody text.\n"

MISSING_NAME_MD = textwrap.dedent("""\
    ---
    description: No name given.
    ---

    Body.
""")

MISSING_DESCRIPTION_MD = textwrap.dedent("""\
    ---
    name: No Description
    ---

    Body.
""")

BAD_TOOLS_TYPE_MD = textwrap.dedent("""\
    ---
    name: Bad Tools
    description: Tools is a string, not a list.
    tools: pytest
    ---

    Body.
""")

EMPTY_BODY_MD = textwrap.dedent("""\
    ---
    name: Empty Body
    description: Frontmatter only, no prompt.
    ---
""")


# ---------------------------------------------------------------------------
# parse_agent_file
# ---------------------------------------------------------------------------


class TestParseAgentFile:
    def test_parses_full_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "security-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.name == "Security Reviewer"
        assert result.description == "Audits pull requests for security vulnerabilities."
        assert "Audit every diff" in result.system_prompt

    def test_tools_frontmatter_survives_parsing(self, tmp_path: Path) -> None:
        """core/plugins_core/skill_md.py drops `tools:` entirely - this reader must not."""
        f = tmp_path / "security-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.tools == ["ruff", "mypy", "semgrep"]

    def test_model_frontmatter_survives_parsing(self, tmp_path: Path) -> None:
        f = tmp_path / "security-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.model == "opus"

    def test_optional_fields_default_when_absent(self, tmp_path: Path) -> None:
        f = tmp_path / "minimal.md"
        f.write_text(MINIMAL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.tools == []
        assert result.model == ""

    def test_body_after_frontmatter_becomes_system_prompt(self, tmp_path: Path) -> None:
        f = tmp_path / "minimal.md"
        f.write_text(MINIMAL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.system_prompt.strip() == "Just the basics."

    def test_role_inferred_from_name_and_description(self, tmp_path: Path) -> None:
        f = tmp_path / "security-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.role == "security"

    def test_id_is_plugin_prefixed_slug(self, tmp_path: Path) -> None:
        f = tmp_path / "security-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.id == "plugin:security-reviewer"

    def test_source_is_plugin(self, tmp_path: Path) -> None:
        f = tmp_path / "security-reviewer.md"
        f.write_text(FULL_AGENT_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, CatalogAgent)
        assert result.source == "plugin"

    def test_malformed_frontmatter_is_a_named_error_not_a_skip(self, tmp_path: Path) -> None:
        """An unterminated frontmatter fence must surface as a structured error.

        Not `None`, not an empty list entry that silently vanishes from the
        catalog count - a named, path-and-reason-carrying error object.
        """
        f = tmp_path / "broken.md"
        f.write_text(UNTERMINATED_FENCE_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)
        assert result.path == str(f)
        assert "fence" in result.reason

    def test_no_frontmatter_fence_is_a_named_error(self, tmp_path: Path) -> None:
        f = tmp_path / "no-fence.md"
        f.write_text(NO_FENCE_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)

    def test_invalid_yaml_is_a_named_error(self, tmp_path: Path) -> None:
        f = tmp_path / "invalid-yaml.md"
        f.write_text(INVALID_YAML_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)

    def test_missing_name_field_is_a_named_error(self, tmp_path: Path) -> None:
        f = tmp_path / "missing-name.md"
        f.write_text(MISSING_NAME_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)
        assert "name" in result.reason

    def test_missing_description_field_is_a_named_error(self, tmp_path: Path) -> None:
        f = tmp_path / "missing-description.md"
        f.write_text(MISSING_DESCRIPTION_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)
        assert "description" in result.reason

    def test_tools_wrong_type_is_a_named_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad-tools.md"
        f.write_text(BAD_TOOLS_TYPE_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)
        assert "tools" in result.reason

    def test_empty_body_is_a_named_error(self, tmp_path: Path) -> None:
        f = tmp_path / "empty-body.md"
        f.write_text(EMPTY_BODY_MD, encoding="utf-8")
        result = parse_agent_file(f)
        assert isinstance(result, AgentDefinitionError)


# ---------------------------------------------------------------------------
# load_plugin_catalog
# ---------------------------------------------------------------------------


class TestLoadPluginCatalog:
    def test_standalone_claude_agents_dir_is_discovered(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        assert [a.name for a in result.agents] == ["Security Reviewer"]
        assert result.errors == []

    def test_plugins_dir_agents_are_discovered(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "plugins" / "review-tools" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reviewer.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        assert [a.name for a in result.agents] == ["Security Reviewer"]

    def test_marketplace_json_scopes_which_plugins_load(self, tmp_path: Path) -> None:
        (tmp_path / "plugins" / "allowed" / "agents").mkdir(parents=True)
        (tmp_path / "plugins" / "allowed" / "agents" / "a.md").write_text(FULL_AGENT_MD, encoding="utf-8")
        (tmp_path / "plugins" / "excluded" / "agents").mkdir(parents=True)
        (tmp_path / "plugins" / "excluded" / "agents" / "b.md").write_text(MINIMAL_AGENT_MD, encoding="utf-8")

        marketplace_dir = tmp_path / ".claude-plugin"
        marketplace_dir.mkdir()
        (marketplace_dir / "marketplace.json").write_text('{"plugins": [{"name": "allowed"}]}', encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        assert [a.name for a in result.agents] == ["Security Reviewer"]
        assert result.errors == []

    def test_missing_marketplace_json_scans_all_plugins(self, tmp_path: Path) -> None:
        (tmp_path / "plugins" / "one" / "agents").mkdir(parents=True)
        (tmp_path / "plugins" / "one" / "agents" / "a.md").write_text(FULL_AGENT_MD, encoding="utf-8")
        (tmp_path / "plugins" / "two" / "agents").mkdir(parents=True)
        (tmp_path / "plugins" / "two" / "agents" / "b.md").write_text(MINIMAL_AGENT_MD, encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        assert {a.name for a in result.agents} == {"Security Reviewer", "Minimal Agent"}

    def test_malformed_marketplace_json_is_a_named_error_and_falls_back_to_scanning(self, tmp_path: Path) -> None:
        (tmp_path / "plugins" / "one" / "agents").mkdir(parents=True)
        (tmp_path / "plugins" / "one" / "agents" / "a.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        marketplace_dir = tmp_path / ".claude-plugin"
        marketplace_dir.mkdir()
        (marketplace_dir / "marketplace.json").write_text("{not valid json", encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        # The broken index is reported, not silently ignored ...
        assert any("marketplace.json" in e.path for e in result.errors)
        # ... but a broken index must not blind the loader to agents that
        # are actually on disk.
        assert [a.name for a in result.agents] == ["Security Reviewer"]

    def test_standalone_and_plugin_sources_merge_in_one_result(self, tmp_path: Path) -> None:
        standalone_dir = tmp_path / ".claude" / "agents"
        standalone_dir.mkdir(parents=True)
        (standalone_dir / "standalone.md").write_text(FULL_AGENT_MD, encoding="utf-8")

        plugin_agents_dir = tmp_path / "plugins" / "extra" / "agents"
        plugin_agents_dir.mkdir(parents=True)
        (plugin_agents_dir / "plugin-agent.md").write_text(MINIMAL_AGENT_MD, encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        assert {a.name for a in result.agents} == {"Security Reviewer", "Minimal Agent"}

    def test_errors_are_collected_not_raised(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "good.md").write_text(FULL_AGENT_MD, encoding="utf-8")
        (agents_dir / "bad.md").write_text(MISSING_NAME_MD, encoding="utf-8")

        result = load_plugin_catalog(tmp_path)

        assert [a.name for a in result.agents] == ["Security Reviewer"]
        assert len(result.errors) == 1
        assert result.errors[0].path.endswith("bad.md")

    def test_empty_root_returns_empty_result_not_an_error(self, tmp_path: Path) -> None:
        result = load_plugin_catalog(tmp_path)
        assert result.agents == []
        assert result.errors == []
