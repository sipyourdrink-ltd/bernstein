"""Unit tests for ``bernstein.core.knowledge.agents_md_generator``.

Covers:

* Each ``_build_*`` section builder against ``tmp_path`` fixture repos.
* ``generate()`` ordering / omission of empty sections.
* ``render_canonical()`` shape (no frontmatter, stable spacing).
* Helpers: ``_first_docstring_line``, ``_first_paragraph``,
  ``_looks_like_nav_strip``, ``_render_two_column_table``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from bernstein.core.knowledge.agents_md_generator import (
    MODULE_MAP_PAGE,
    AgentsMdSection,
    GenerateOptions,
    _first_docstring_line,
    _first_paragraph,
    _looks_like_nav_strip,
    _render_two_column_table,
    generate,
    render_canonical,
    render_module_map_page,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_repo(root: Path, *, with_git: bool = False) -> Path:
    """Create a minimal Python project skeleton under ``root``."""
    (root / "src" / "bernstein" / "core").mkdir(parents=True)
    (root / "src" / "bernstein" / "core" / "__init__.py").write_text("")
    (root / "src" / "bernstein" / "core" / "models.py").write_text(
        '"""Domain model dataclasses used across the orchestrator."""\n'
    )
    (root / "src" / "bernstein" / "core" / "router.py").write_text('"""Cost-aware model router."""\n')
    (root / "templates" / "roles" / "backend").mkdir(parents=True)
    (root / "templates" / "roles" / "backend" / "system_prompt.md").write_text("# Backend role")
    (root / "templates" / "roles" / "qa").mkdir(parents=True)
    (root / "templates" / "roles" / "qa" / "system_prompt.md").write_text("# QA role")
    (root / "README.md").write_text("# Demo\n\nA tiny demo project for unit tests.\n")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\n[project.scripts]\ndemo = 'demo.cli:main'\n"
    )
    if with_git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


# ---------------------------------------------------------------------------
# generate() - top-level behaviour
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_returns_empty_for_missing_repo(self, tmp_path: Path) -> None:
        assert generate(tmp_path / "does-not-exist") == []

    def test_produces_sections_in_canonical_order(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        keys = [s.key for s in sections]
        # Order: overview before module-map before build-test before setup
        # before architecture before roles. Conventions/git-workflow may be
        # absent (no overlay file / no git).
        seen_keys = [k for k in keys if k in {"overview", "module-map", "build-test", "setup", "architecture", "roles"}]
        assert seen_keys == ["overview", "module-map", "build-test", "setup", "architecture", "roles"]

    def test_omits_empty_sections_silently(self, tmp_path: Path) -> None:
        # Repo without README, without pyproject, without templates/roles.
        (tmp_path / "src" / "bernstein").mkdir(parents=True)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        keys = {s.key for s in sections}
        assert "overview" not in keys
        assert "build-test" not in keys
        assert "roles" not in keys

    def test_overlay_section_appears_after_builtins(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        overlay = tmp_path / ".sdd" / "agents-md"
        overlay.mkdir(parents=True)
        (overlay / "deployment.md").write_text("# Deployment\n\nWe deploy via `make deploy`.\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        keys = [s.key for s in sections]
        assert keys[-1] == "deployment"
        assert sections[-1].title == "Deployment"
        assert "make deploy" in sections[-1].body

    def test_conventions_overlay_consumed_as_section(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        overlay = tmp_path / ".sdd" / "agents-md"
        overlay.mkdir(parents=True)
        (overlay / "conventions.md").write_text("Use snake_case for functions, CamelCase for classes.\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        conv = next(s for s in sections if s.kind == "conventions")
        assert "snake_case" in conv.body
        # Conventions does NOT also leak as a custom overlay.
        custom_keys = {s.key for s in sections if s.kind == "custom"}
        assert "conventions" not in custom_keys

    def test_include_git_workflow_false_skips_section(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, with_git=True)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        assert all(s.kind != "git-workflow" for s in sections)

    def test_include_module_map_false_skips_section(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_module_map=False))
        assert all(s.kind != "module-map" for s in sections)


# ---------------------------------------------------------------------------
# render_canonical() - shape contracts
# ---------------------------------------------------------------------------


class TestRenderCanonical:
    def test_starts_with_h1_and_no_frontmatter(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        out = render_canonical(sections, repo_name="demo")
        assert out.splitlines()[0] == "# demo - AGENTS.md"
        # No YAML frontmatter - agents.md spec is explicitly schema-free.
        assert not out.startswith("---")

    def test_section_headings_use_h2(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        out = render_canonical(sections, repo_name="demo")
        # Every section must render as `## Title` - never H3 or H1.
        for sec in sections:
            assert f"\n## {sec.title}\n" in out

    def test_ends_with_single_newline(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        out = render_canonical(sections, repo_name="demo")
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_repo_name_default_is_neutral(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        out = render_canonical(sections)
        assert "Project - AGENTS.md" in out


# ---------------------------------------------------------------------------
# Section builder edges - module map preserves gen_agents_md.py contract
# ---------------------------------------------------------------------------


class TestModuleMap:
    """The embedded section (#4142): a compact one-row-per-package index.

    Per-file docstrings moved to :func:`render_module_map_page`'s output -
    see ``TestModuleMapPage`` below - so this class checks the index shape,
    not file-level content.
    """

    def test_one_row_per_top_level_package_present(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        mm = next(s for s in sections if s.kind == "module-map")
        # The fixture only populates src/bernstein/core/, so that's the one
        # package row this repo shape can produce.
        assert "`core/`" in mm.body
        assert "orchestration engine" in mm.body

    def test_does_not_embed_per_file_docstrings(self, tmp_path: Path) -> None:
        """The regression this whole issue exists to fix (#4142)."""
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        mm = next(s for s in sections if s.kind == "module-map")
        assert "Cost-aware model router" not in mm.body
        assert "Domain model dataclasses" not in mm.body

    def test_links_to_the_full_page(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        mm = next(s for s in sections if s.kind == "module-map")
        assert MODULE_MAP_PAGE in mm.body

    def test_package_without_own_agents_md_points_at_directory(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        mm = next(s for s in sections if s.kind == "module-map")
        assert "`src/bernstein/core/`" in mm.body

    def test_package_with_own_agents_md_points_at_it(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, with_git=True)
        core = tmp_path / "src" / "bernstein" / "core"
        (core / "AGENTS.md").write_text("# core context\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add core AGENTS.md"], cwd=tmp_path, check=True)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        mm = next(s for s in sections if s.kind == "module-map")
        assert "`src/bernstein/core/AGENTS.md`" in mm.body
        # The bare-directory fallback string does not also appear - the
        # closing backtick lands right after "core/" only in the fallback
        # form, not inside "...core/AGENTS.md`".
        assert "`src/bernstein/core/`" not in mm.body


class TestModuleMapPage:
    """:func:`render_module_map_page` - the full per-file table's new home."""

    def test_picks_up_module_docstrings(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        page = render_module_map_page(tmp_path)
        assert page is not None
        assert "Cost-aware model router" in page
        assert "Domain model dataclasses" in page

    def test_none_when_no_src_tree(self, tmp_path: Path) -> None:
        assert render_module_map_page(tmp_path) is None

    def test_carries_autogen_marker(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        page = render_module_map_page(tmp_path)
        assert page is not None
        assert "AUTO-GENERATED by `bernstein agents-md sync`" in page

    def test_truncation_when_over_max(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        # Add 30 extra modules to force truncation.
        core = tmp_path / "src" / "bernstein" / "core"
        for i in range(30):
            (core / f"mod_{i:02d}.py").write_text(f'"""Module {i}."""\n')
        page = render_module_map_page(tmp_path, GenerateOptions(max_module_map_lines=5))
        assert page is not None
        # Truncation marker present.
        assert "more_" in page or "truncated" in page


class TestModuleMapLineBudget:
    """#4142's own line-budget claim, demonstrated rather than asserted blind.

    A package's row count in the *old* per-file table scaled with its file
    count; the compact index is always one row per package regardless.
    Piling files into ``core/`` makes ``render_module_map_page`` (the old
    table's new home) balloon well past any reasonable line budget while
    ``render_canonical`` - which now embeds the compact index, not the
    table - stays flat. That contrast is the property the issue's
    acceptance criteria call "failing before your change and passing
    after": these two assertions are what would have disagreed before
    #4142 and agree now.
    """

    _FILE_COUNT = 200
    _LINE_BUDGET = 160

    def _make_oversized_repo(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        core = tmp_path / "src" / "bernstein" / "core"
        for i in range(self._FILE_COUNT):
            (core / f"mod_{i:03d}.py").write_text(f'"""Module {i}."""\n')

    def test_full_page_would_blow_the_budget(self, tmp_path: Path) -> None:
        self._make_oversized_repo(tmp_path)
        page = render_module_map_page(tmp_path)
        assert page is not None
        assert page.count("\n") > self._LINE_BUDGET

    def test_compact_canonical_render_stays_under_budget(self, tmp_path: Path) -> None:
        self._make_oversized_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        out = render_canonical(sections, repo_name="demo")
        assert out.count("\n") <= self._LINE_BUDGET


class TestSyncDeterminism:
    """#4142's other explicit test requirement: two consecutive runs agree."""

    def test_two_consecutive_renders_are_byte_identical(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        opts = GenerateOptions(include_git_workflow=False)
        first = render_canonical(generate(tmp_path, opts), repo_name="demo")
        second = render_canonical(generate(tmp_path, opts), repo_name="demo")
        assert first == second

    def test_two_consecutive_module_map_pages_are_byte_identical(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        first = render_module_map_page(tmp_path)
        second = render_module_map_page(tmp_path)
        assert first == second


class TestBuildTestSection:
    def test_fails_silent_when_no_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "bernstein").mkdir(parents=True)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        assert all(s.kind != "build-test" for s in sections)

    def test_pyproject_uv_yields_uv_commands(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        # Add a uv config marker so the builder picks the uv branch.
        (tmp_path / "uv.lock").write_text("# minimal\n")
        pyp = tmp_path / "pyproject.toml"
        pyp.write_text(pyp.read_text() + "[tool.uv]\nfoo = 1\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        bt = next(s for s in sections if s.kind == "build-test")
        assert "uv sync" in bt.body
        assert "uv run pytest" in bt.body

    def test_uv_lock_alone_triggers_uv_branch(self, tmp_path: Path) -> None:
        # A hatchling-built repo can be uv-managed with no ``[tool.uv]`` table;
        # the sole signal is ``uv.lock``. It must still render uv commands so
        # the install/test lines agree with the uv run lint/type-check lines.
        _make_repo(tmp_path)
        (tmp_path / "uv.lock").write_text("# minimal\n")
        pyp = tmp_path / "pyproject.toml"
        pyp.write_text(pyp.read_text() + "[tool.ruff]\nline-length = 120\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        bt = next(s for s in sections if s.kind == "build-test")
        assert "uv sync" in bt.body
        assert "pip install" not in bt.body
        # Lint line uses the same runner prefix, not a bare ``ruff``.
        assert "uv run ruff check ." in bt.body

    def test_isolated_test_runner_preferred_over_bare_pytest(self, tmp_path: Path) -> None:
        # When the repo ships scripts/run_tests.py, the build-test block must
        # emit that isolated per-file runner rather than a bare ``pytest`` that
        # would retain the whole suite in memory.
        _make_repo(tmp_path)
        (tmp_path / "uv.lock").write_text("# minimal\n")
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.py").write_text("# isolated runner\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        bt = next(s for s in sections if s.kind == "build-test")
        assert "uv run python scripts/run_tests.py" in bt.body
        # No standalone bare ``pytest`` command line.
        assert not re.search(r"(?m)^(uv run )?pytest\b", bt.body)


class TestArchitectureSection:
    def test_picks_up_project_scripts(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        arch = next((s for s in sections if s.kind == "architecture"), None)
        assert arch is not None
        assert "demo" in arch.body
        assert "demo.cli:main" in arch.body


class TestRolesSection:
    def test_lists_roles_alphabetically(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        roles = next(s for s in sections if s.kind == "roles")
        # Both shipped roles appear; backend before qa alphabetically.
        idx_backend = roles.body.index("`backend`")
        idx_qa = roles.body.index("`qa`")
        assert idx_backend < idx_qa


class TestGitWorkflowSection:
    def test_omitted_outside_git_repo(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, with_git=False)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=True))
        assert all(s.kind != "git-workflow" for s in sections)

    def test_present_inside_git_repo(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, with_git=True)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=True))
        gw = next((s for s in sections if s.kind == "git-workflow"), None)
        assert gw is not None
        assert "Default branch" in gw.body


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestFirstDocstringLine:
    def test_module_with_docstring(self, tmp_path: Path) -> None:
        path = tmp_path / "m.py"
        path.write_text('"""First line.\n\nSecond paragraph."""\n')
        assert _first_docstring_line(path) == "First line"

    def test_module_without_docstring(self, tmp_path: Path) -> None:
        path = tmp_path / "m.py"
        path.write_text("import os\n")
        assert _first_docstring_line(path) == ""

    def test_invalid_python_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "m.py"
        path.write_text("def broken(:\n    ...\n")
        assert _first_docstring_line(path) == ""


class TestFirstParagraph:
    def test_skips_h1_then_returns_prose(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# Title\n\nReal prose paragraph here.\n")
        assert _first_paragraph(readme) == "Real prose paragraph here."

    def test_skips_badge_strip(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Title\n\n"
            "[![Build](https://x/y.svg)](https://x/y) "
            "[![Coverage](https://x/c.svg)](https://x/c)\n\n"
            "Actual prose intro.\n"
        )
        assert "Actual prose intro" in _first_paragraph(readme)

    def test_skips_horizontal_link_strip(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Title\n\n"
            "[Docs](docs.md) | [Install](install.md) | [Changelog](changelog.md)\n\n"
            "Prose only after the nav.\n"
        )
        assert _first_paragraph(readme) == "Prose only after the nav."


class TestNavStripDetector:
    def test_three_links_with_pipes_is_nav(self) -> None:
        assert _looks_like_nav_strip("[A](u1) | [B](u2) | [C](u3)") is True

    def test_three_links_with_middot_is_nav(self) -> None:
        assert _looks_like_nav_strip("[A](u1) · [B](u2) · [C](u3)") is True

    def test_two_links_is_not_nav(self) -> None:
        assert _looks_like_nav_strip("See [foo](u1) and [bar](u2) for details.") is False

    def test_pure_prose_is_not_nav(self) -> None:
        assert _looks_like_nav_strip("Bernstein orchestrates AI coding agents.") is False


class TestRenderTwoColumnTable:
    def test_pads_left_column_to_max_width(self) -> None:
        out = _render_two_column_table([("a", "x"), ("longer_name", "y")], "Name")
        lines = out.splitlines()
        # Header row width matches separator row width.
        assert len(lines[0]) == len(lines[1])
        # Both data rows have the same left-column *padded* width
        # (preserve trailing whitespace; that's the padding under test).
        col1_a = lines[2].split("|")[1]
        col1_b = lines[3].split("|")[1]
        assert len(col1_a) == len(col1_b)
        assert col1_a.strip() == "a"
        assert col1_b.strip() == "longer_name"

    def test_empty_returns_empty(self) -> None:
        assert _render_two_column_table([], "X") == ""


# ---------------------------------------------------------------------------
# AgentsMdSection - frozen + minimal
# ---------------------------------------------------------------------------


class TestAgentsMdSection:
    def test_frozen(self) -> None:
        s = AgentsMdSection(key="k", title="T", body="b", kind="overview")
        # FrozenInstanceError subclasses AttributeError; use the more specific
        # parent so we don't paper over an unrelated runtime error.
        with pytest.raises(AttributeError):
            s.body = "new"  # type: ignore[misc]

    def test_default_globs_empty_and_always_apply_true(self) -> None:
        s = AgentsMdSection(key="k", title="T", body="b", kind="overview")
        assert s.target_globs == ()
        assert s.always_apply is True


# ---------------------------------------------------------------------------
# _build_directory_context - nested AGENTS.md map
# ---------------------------------------------------------------------------


class TestBuildDirectoryContext:
    def test_absent_when_no_nested_files(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        assert all(s.key != "directory-context" for s in sections)

    def test_lists_nested_file_with_h1_as_description(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        (tmp_path / "src" / "bernstein" / "core" / "AGENTS.md").write_text("# Core engine\n\nInvariants live here.\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        sec = next(s for s in sections if s.key == "directory-context")
        assert "`src/bernstein/core/AGENTS.md`" in sec.body
        assert "Core engine" in sec.body
        assert sec.always_apply is False

    def test_rows_sorted_by_posix_path(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "AGENTS.md").write_text("# Test suite\n")
        (tmp_path / "src" / "bernstein" / "core" / "AGENTS.md").write_text("# Core\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        sec = next(s for s in sections if s.key == "directory-context")
        assert sec.body.index("src/bernstein/core/AGENTS.md") < sec.body.index("tests/AGENTS.md")

    def test_missing_h1_falls_back_to_directory_name(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        (tmp_path / "src" / "bernstein" / "core" / "AGENTS.md").write_text("no heading here\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        sec = next(s for s in sections if s.key == "directory-context")
        assert "core/ context" in sec.body

    def test_appears_between_module_map_and_build_test(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        (tmp_path / "src" / "bernstein" / "core" / "AGENTS.md").write_text("# Core\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        keys = [s.key for s in sections]
        assert keys.index("module-map") < keys.index("directory-context") < keys.index("build-test")

    def test_untracked_file_excluded_inside_git_repo(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, with_git=True)
        tracked = tmp_path / "src" / "bernstein" / "core" / "AGENTS.md"
        tracked.write_text("# Core engine\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "core context"], cwd=tmp_path, check=True)
        # Untracked scratch file must not leak into the render.
        (tmp_path / "src" / "bernstein" / "AGENTS.md").write_text("# Scratch notes\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        sec = next(s for s in sections if s.key == "directory-context")
        assert "`src/bernstein/core/AGENTS.md`" in sec.body
        assert "Scratch notes" not in sec.body

    def test_only_untracked_files_omits_section_inside_git_repo(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, with_git=True)
        (tmp_path / "src" / "bernstein" / "core" / "AGENTS.md").write_text("# Never committed\n")
        sections = generate(tmp_path, GenerateOptions(include_git_workflow=False))
        assert all(s.key != "directory-context" for s in sections)
