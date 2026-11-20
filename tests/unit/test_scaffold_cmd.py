"""Tests for the prompted-app scaffold generator.

Covers:
* Template registry contract - every template renders into a non-empty
  file tree using only the documented placeholders.
* Heuristic ``pick_template`` - keyword routing is deterministic and
  case-insensitive, with a sensible default fallback.
* ``materialize_template`` - produces the expected file tree on disk and
  refuses to overwrite a non-empty target without ``--force``.
* CLI surface - ``bernstein scaffold "<prompt>"`` exits 0, picks a
  template, and writes the expected files. Unknown templates exit non-zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.scaffold_cmd import scaffold_cmd
from bernstein.cli.main import cli
from bernstein.cli.scaffold.templates import (
    SCAFFOLD_TEMPLATES,
    ScaffoldError,
    list_template_names,
    materialize_template,
    pick_template,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    """Each bundled template is well-formed and discoverable."""

    def test_three_templates_present(self) -> None:
        assert sorted(SCAFFOLD_TEMPLATES.keys()) == [
            "node-cli",
            "python-cli",
            "static-site",
        ]

    def test_list_template_names_is_sorted(self) -> None:
        names = list_template_names()
        assert names == sorted(names)
        assert "python-cli" in names

    def test_each_template_has_files_and_keywords(self) -> None:
        for tmpl in SCAFFOLD_TEMPLATES.values():
            assert tmpl.name
            assert tmpl.description
            assert len(tmpl.keywords) >= 3
            assert len(tmpl.files) >= 2

    def test_templates_declare_a_readme(self) -> None:
        for tmpl in SCAFFOLD_TEMPLATES.values():
            relative_paths = {f.relative_path for f in tmpl.files}
            assert any("README" in p for p in relative_paths), tmpl.name


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------


class TestPickTemplate:
    """Keyword heuristic is deterministic and case-insensitive."""

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("Build me a Python CLI to convert PDFs", "python-cli"),
            ("Make a Streamlit dashboard for sales data", "python-cli"),
            ("Build a Next.js app", "node-cli"),
            ("a quick npm script for ETL", "node-cli"),
            ("static landing page with Tailwind", "static-site"),
            ("personal portfolio site", "static-site"),
        ],
    )
    def test_keyword_routing(self, prompt: str, expected: str) -> None:
        assert pick_template(prompt).name == expected

    def test_default_fallback_when_no_keywords_match(self) -> None:
        # Pure noise word with no overlap on any template keyword list.
        assert pick_template("zzz qqq xyzxyz").name == "python-cli"

    def test_case_insensitive(self) -> None:
        assert pick_template("STATIC LANDING PAGE").name == "static-site"

    def test_deterministic_under_ties(self) -> None:
        # Many calls on the same prompt must return the same template.
        prompt = "build a python and node hybrid"
        choices = {pick_template(prompt).name for _ in range(10)}
        assert len(choices) == 1


# ---------------------------------------------------------------------------
# Materialise
# ---------------------------------------------------------------------------


class TestMaterializeTemplate:
    """Render templates to disk."""

    def test_python_cli_produces_expected_tree(self, tmp_path: Path) -> None:
        dest = tmp_path / "habit-tracker"
        template = SCAFFOLD_TEMPLATES["python-cli"]
        written = materialize_template(template, dest, prompt="Build me a habit tracker")

        relative = sorted(p.relative_to(dest).as_posix() for p in written)
        assert relative == [
            "README.md",
            "pyproject.toml",
            "src/habit-tracker/__init__.py",
            "src/habit-tracker/main.py",
        ]

        pyproject = (dest / "pyproject.toml").read_text()
        assert 'name = "habit-tracker"' in pyproject
        readme = (dest / "README.md").read_text()
        assert "Build me a habit tracker" in readme
        assert "habit-tracker" in readme

    def test_node_cli_produces_package_json(self, tmp_path: Path) -> None:
        dest = tmp_path / "todo-app"
        template = SCAFFOLD_TEMPLATES["node-cli"]
        materialize_template(template, dest, prompt="Tiny TODO web app")

        package_json = (dest / "package.json").read_text()
        assert '"name": "todo-app"' in package_json
        assert (dest / "bin" / "cli.js").exists()

    def test_static_site_produces_html(self, tmp_path: Path) -> None:
        dest = tmp_path / "landing"
        template = SCAFFOLD_TEMPLATES["static-site"]
        materialize_template(template, dest, prompt="Marketing landing page")

        html = (dest / "index.html").read_text()
        assert "<title>landing</title>" in html
        assert "Marketing landing page" in html
        assert (dest / "style.css").exists()

    def test_refuses_non_empty_dest_without_force(self, tmp_path: Path) -> None:
        dest = tmp_path / "occupied"
        dest.mkdir()
        (dest / "preexisting.txt").write_text("hi")

        with pytest.raises(ScaffoldError, match="already exists"):
            materialize_template(
                SCAFFOLD_TEMPLATES["python-cli"],
                dest,
                prompt="anything",
            )

    def test_force_overwrites_non_empty_dest(self, tmp_path: Path) -> None:
        dest = tmp_path / "occupied"
        dest.mkdir()
        (dest / "preexisting.txt").write_text("hi")

        materialize_template(
            SCAFFOLD_TEMPLATES["python-cli"],
            dest,
            prompt="anything",
            force=True,
        )
        assert (dest / "pyproject.toml").exists()
        assert (dest / "preexisting.txt").exists()  # untouched


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestScaffoldCli:
    """Click CLI behaviour."""

    def test_cli_scaffolds_via_auto_pick(self, tmp_path: Path) -> None:
        runner = CliRunner()
        dest = tmp_path / "out"
        result = runner.invoke(
            scaffold_cmd,
            ["Build me a habit tracker", "--output", str(dest)],
        )

        assert result.exit_code == 0, result.output
        assert "Scaffolded" in result.output
        assert (dest / "pyproject.toml").exists()
        assert (dest / "README.md").exists()

    def test_cli_explicit_template(self, tmp_path: Path) -> None:
        runner = CliRunner()
        dest = tmp_path / "site"
        result = runner.invoke(
            scaffold_cmd,
            [
                "Whatever",
                "--template",
                "static-site",
                "--output",
                str(dest),
            ],
        )

        assert result.exit_code == 0, result.output
        assert (dest / "index.html").exists()
        assert (dest / "style.css").exists()

    def test_cli_requires_force_to_overwrite_nonempty_output(self, tmp_path: Path) -> None:
        dest = tmp_path / "site"
        dest.mkdir()
        page = dest / "index.html"
        existing_content = "<h1>Existing site</h1>"
        page.write_text(existing_content, encoding="utf-8")
        args = ["scaffold", "Garden notebook", "--template", "static-site", "--output", str(dest)]
        runner = CliRunner()

        refused = runner.invoke(cli, args)

        assert refused.exit_code == 1, refused.output
        assert "--force" in refused.output
        assert page.read_text(encoding="utf-8") == existing_content
        assert not (dest / "style.css").exists()

        forced = runner.invoke(cli, [*args, "--force"])

        assert forced.exit_code == 0, forced.output
        assert "Garden notebook" in page.read_text(encoding="utf-8")
        assert (dest / "style.css").is_file()

    def test_cli_unknown_template_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            scaffold_cmd,
            [
                "Build me anything",
                "--template",
                "definitely-not-a-template",
                "--output",
                str(tmp_path / "dst"),
            ],
        )

        assert result.exit_code != 0
        assert "Unknown template" in result.output

    def test_cli_default_output_uses_slug(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                scaffold_cmd,
                ["Build me a Habit Tracker!"],
            )
            assert result.exit_code == 0, result.output
            assert Path("build-me-a-habit-tracker").is_dir()
