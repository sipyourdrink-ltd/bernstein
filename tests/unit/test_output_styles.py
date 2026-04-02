"""Tests for output_styles — output style loading and rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.output_styles import OutputStyle, StyleConfig, load_output_styles, load_style

# --- Fixtures ---


@pytest.fixture()
def project_with_styles(tmp_path: Path) -> Path:
    """Create a project dir with .bernstein/output-styles/."""
    styles_dir = tmp_path / ".bernstein" / "output-styles"
    styles_dir.mkdir(parents=True)
    return tmp_path


# --- TestOutputStyle ---


class TestOutputStyle:
    def test_render_prompt_basic(self) -> None:
        s = OutputStyle(name="compact", description="Short output")
        prompt = s.render_prompt()
        assert "compact" in prompt
        assert "Short output" in prompt

    def test_render_no_coding_instructions(self) -> None:
        s = OutputStyle(name="terse", keep_coding_instructions=False)
        prompt = s.render_prompt()
        assert "Do NOT include coding instructions" in prompt

    def test_render_terse_mode(self) -> None:
        s = OutputStyle(name="terse", terse_mode=True)
        prompt = s.render_prompt()
        assert "terse" in prompt.lower()

    def test_render_suppress_progress(self) -> None:
        s = OutputStyle(name="quiet", suppress_progress=True)
        prompt = s.render_prompt()
        assert "Suppress incremental progress" in prompt


# --- TestStyleConfig ---


class TestStyleConfig:
    def test_empty_has_no_prompt(self) -> None:
        config = StyleConfig()
        assert config.get_prompt() == ""

    def test_active_style_returns_prompt(self) -> None:
        config = StyleConfig(active_style=OutputStyle(name="test"))
        assert "test" in config.get_prompt()


# --- TestLoadStyle ---


class TestLoadStyle:
    def test_load_from_file_with_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "compact.md"
        f.write_text("---\nname: compact\ndescription: minimal output\n---\n")
        s = load_style(f)
        assert s is not None
        assert s.name == "compact"
        assert "minimal output" in s.description

    def test_load_from_file_without_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "plain.md"
        f.write_text("# Plain style\nJust regular output")
        s = load_style(f)
        assert s is not None
        assert s.name == "plain"  # defaults to filename stem

    def test_load_nonexistent_file(self) -> None:
        assert load_style(Path("/nonexistent/missing.md")) is None


# --- TestLoadOutputStyles ---


class TestLoadOutputStyles:
    def test_no_styles_dir(self, tmp_path: Path) -> None:
        config = load_output_styles(tmp_path)
        assert config.active_style is None
        assert config.available == []

    def test_loads_default_files(self, project_with_styles: Path) -> None:
        styles_dir = project_with_styles / ".bernstein" / "output-styles"
        (styles_dir / "compact.md").write_text("---\nname: compact\n---\n")
        (styles_dir / "terse.md").write_text("---\nname: terse\nterse_mode: true\n---\n")
        config = load_output_styles(project_with_styles)
        assert len(config.available) == 2
        assert config.active_style is not None
        assert config.active_style.name == "compact"  # first loaded

    def test_picks_preferred_from_bernstein_yaml(self, project_with_styles: Path) -> None:
        styles_dir = project_with_styles / ".bernstein" / "output-styles"
        (styles_dir / "compact.md").write_text("---\nname: compact\n---\n")
        (styles_dir / "terse.md").write_text("---\nname: terse\nterse_mode: true\n---\n")
        (project_with_styles / "bernstein.yaml").write_text("output_style: terse\n")
        config = load_output_styles(project_with_styles)
        assert config.active_style is not None
        assert config.active_style.name == "terse"
        assert config.active_style.terse_mode is True

    def test_extras_loaded_after_defaults(self, project_with_styles: Path) -> None:
        styles_dir = project_with_styles / ".bernstein" / "output-styles"
        (styles_dir / "compact.md").write_text("---\nname: compact\n---\n")
        (styles_dir / "my-style.md").write_text("---\nname: my-style\n---\n")
        config = load_output_styles(project_with_styles)
        names = [s.name for s in config.available]
        assert "compact" in names
        assert "my-style" in names
