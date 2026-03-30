"""Tests for bernstein.templates.renderer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.templates.renderer import (
    _DEFAULT_TEMPLATES_DIR,
    render_role_prompt,
    render_template,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def templates_dir(tmp_path: Path) -> Path:
    """Create a minimal role template tree under tmp_path."""
    role_dir = tmp_path / "manager"
    role_dir.mkdir()
    (role_dir / "system_prompt.md").write_text(
        "# Manager\nGoal: {{GOAL}}\nTeam size: {{CELL_SIZE}}\n"
        "{{#IF BUDGET}}Budget: {{BUDGET}}\n{{/IF}}"
        "{{#IF_NOT BUDGET}}No budget set.\n{{/IF_NOT}}"
    )
    return tmp_path


@pytest.fixture()
def simple_template(tmp_path: Path) -> Path:
    """Write a simple template file and return its path."""
    p = tmp_path / "simple.md"
    p.write_text("Hello {{NAME}}, welcome to {{PROJECT}}!")
    return p


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------


class TestPlaceholderSubstitution:
    """Tests for {{VAR}} placeholder replacement."""

    def test_simple_substitution(self, simple_template: Path) -> None:
        result = render_template(simple_template, {"NAME": "Alice", "PROJECT": "Bernstein"})
        assert result == "Hello Alice, welcome to Bernstein!"

    def test_unknown_placeholder_left_as_is(self, simple_template: Path) -> None:
        result = render_template(simple_template, {"NAME": "Alice"})
        assert "{{PROJECT}}" in result
        assert "Alice" in result

    def test_empty_string_substitution(self, simple_template: Path) -> None:
        result = render_template(simple_template, {"NAME": "", "PROJECT": "X"})
        assert result == "Hello , welcome to X!"

    def test_multiline_substitution(self, tmp_path: Path) -> None:
        p = tmp_path / "multi.md"
        p.write_text("Line1: {{A}}\nLine2: {{B}}\nLine3: {{A}}")
        result = render_template(p, {"A": "alpha", "B": "beta"})
        assert result == "Line1: alpha\nLine2: beta\nLine3: alpha"


# ---------------------------------------------------------------------------
# Conditional blocks
# ---------------------------------------------------------------------------


class TestConditionals:
    """Tests for {{#IF VAR}} and {{#IF_NOT VAR}} blocks."""

    def test_if_block_included_when_truthy(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("Start {{#IF SHOW}}visible{{/IF}} End")
        result = render_template(p, {"SHOW": "yes"})
        assert result == "Start visible End"

    def test_if_block_excluded_when_falsy(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("Start {{#IF SHOW}}visible{{/IF}} End")
        result = render_template(p, {"SHOW": ""})
        assert result == "Start  End"

    def test_if_block_excluded_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("Start {{#IF SHOW}}visible{{/IF}} End")
        result = render_template(p, {})
        assert result == "Start  End"

    def test_if_not_block_included_when_falsy(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("{{#IF_NOT AUTH}}Please log in.{{/IF_NOT}}")
        result = render_template(p, {})
        assert result == "Please log in."

    def test_if_not_block_excluded_when_truthy(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("{{#IF_NOT AUTH}}Please log in.{{/IF_NOT}}")
        result = render_template(p, {"AUTH": "token123"})
        assert result == ""

    def test_multiline_conditional_body(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("{{#IF NOTES}}Notes:\n{{NOTES}}\n{{/IF}}")
        result = render_template(p, {"NOTES": "Important stuff"})
        assert "Notes:\nImportant stuff\n" in result

    def test_if_and_if_not_together_truthy(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("{{#IF X}}yes{{/IF}}{{#IF_NOT X}}no{{/IF_NOT}}")
        # IF block included, IF_NOT block excluded.
        assert render_template(p, {"X": "1"}) == "yes"

    def test_if_and_if_not_inverse(self, tmp_path: Path) -> None:
        p = tmp_path / "cond.md"
        p.write_text("{{#IF X}}yes{{/IF}}{{#IF_NOT X}}no{{/IF_NOT}}")
        assert render_template(p, {}) == "no"


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


class TestFileHandling:
    """Tests for file-not-found and read errors."""

    def test_missing_template_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Template not found"):
            render_template(tmp_path / "nonexistent.md", {})


# ---------------------------------------------------------------------------
# render_role_prompt
# ---------------------------------------------------------------------------


class TestRenderRolePrompt:
    """Tests for the role prompt convenience wrapper."""

    def test_renders_manager_prompt(self, templates_dir: Path) -> None:
        result = render_role_prompt(
            "manager",
            {"GOAL": "Build an API", "CELL_SIZE": "4", "BUDGET": "$50"},
            templates_dir=templates_dir,
        )
        assert "Build an API" in result
        assert "Team size: 4" in result
        assert "Budget: $50" in result
        assert "No budget set." not in result

    def test_renders_without_budget(self, templates_dir: Path) -> None:
        result = render_role_prompt(
            "manager",
            {"GOAL": "Build an API", "CELL_SIZE": "4"},
            templates_dir=templates_dir,
        )
        assert "No budget set." in result
        assert "Budget:" not in result

    def test_unknown_role_raises_file_not_found(self, templates_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            render_role_prompt("nonexistent_role", {}, templates_dir=templates_dir)

    @pytest.mark.skipif(
        not (_DEFAULT_TEMPLATES_DIR / "manager" / "system_prompt.md").exists(),
        reason="templates/roles/ is gitignored; skip in CI unless bundled",
    )
    def test_uses_real_templates_dir(self) -> None:
        """Smoke test: render the actual manager template without blowing up."""
        result = render_role_prompt(
            "manager",
            {},
        )
        assert "Manager" in result or "manager" in result.lower()
        assert "task server" in result.lower() or "Task Server" in result
