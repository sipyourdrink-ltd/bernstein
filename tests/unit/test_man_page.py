"""Tests for the man page generator."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from bernstein.cli.man_page import (
    generate_all_man_pages,
    generate_man_page,
    write_man_pages,
)


class TestGenerateManPage:
    """Tests for generate_man_page()."""

    def test_starts_with_th_header(self) -> None:
        page = generate_man_page("run", "Start a run.", options=[])
        assert page.startswith(".TH BERNSTEIN-RUN 1")

    def test_has_required_sections(self) -> None:
        page = generate_man_page(
            "status",
            "Show task summary.",
            options=[("--json", "Output as JSON.")],
        )
        assert ".SH NAME" in page
        assert ".SH SYNOPSIS" in page
        assert ".SH DESCRIPTION" in page
        assert ".SH OPTIONS" in page
        assert ".SH SEE ALSO" in page

    def test_name_section_content(self) -> None:
        page = generate_man_page("stop", "Graceful stop.", options=[])
        # NAME section should have the command name and description
        assert "bernstein\\-stop \\- Graceful stop" in page

    def test_options_formatted(self) -> None:
        page = generate_man_page(
            "run",
            "Start a run.",
            options=[
                ("--budget", "Maximum budget in USD."),
                ("--max-agents", "Maximum concurrent agents."),
            ],
        )
        assert ".TP" in page
        assert "\\fB\\-\\-budget\\fR" in page
        assert "Maximum budget in USD." in page
        assert "\\fB\\-\\-max\\-agents\\fR" in page

    def test_subcommands_section(self) -> None:
        page = generate_man_page(
            "agents",
            "Agent management.",
            options=[],
            subcommands=[
                ("list", "List available agents."),
                ("sync", "Pull latest agent catalog."),
            ],
        )
        assert ".SH SUBCOMMANDS" in page
        assert "list" in page
        assert "sync" in page

    def test_no_subcommands_section_when_none(self) -> None:
        page = generate_man_page("run", "Start a run.", options=[])
        assert ".SH SUBCOMMANDS" not in page

    def test_synopsis_shows_options_hint(self) -> None:
        page = generate_man_page("run", "Start.", options=[("--budget", "Budget.")])
        assert "[\\fIOPTIONS\\fR]" in page

    def test_synopsis_shows_command_hint_for_groups(self) -> None:
        page = generate_man_page(
            "agents",
            "Agent management.",
            options=[],
            subcommands=[("list", "List agents.")],
        )
        assert "[\\fICOMMAND\\fR]" in page

    def test_hyphens_escaped(self) -> None:
        page = generate_man_page(
            "self-update",
            "Self-update the CLI.",
            options=[("--no-confirm", "Skip confirmation.")],
        )
        # Hyphens in option names and command names should be escaped
        assert "\\-\\-no\\-confirm" in page
        assert "bernstein\\-self\\-update" in page

    def test_empty_help_text(self) -> None:
        """Commands with empty help should not crash."""
        page = generate_man_page("mystery", "", options=[])
        assert ".TH BERNSTEIN-MYSTERY 1" in page
        assert ".SH NAME" in page

    def test_multiline_description(self) -> None:
        page = generate_man_page(
            "run",
            "Start a run.\n\nThis is a longer description\nthat spans lines.",
            options=[],
        )
        assert ".SH DESCRIPTION" in page
        assert "Start a run." in page
        assert "longer description" in page


class TestGenerateAllManPages:
    """Tests for generate_all_man_pages()."""

    @pytest.fixture()
    def sample_group(self) -> click.Group:
        """Build a small Click group for testing."""

        @click.group()
        def cli() -> None:
            """Test CLI group."""

        @cli.command()
        @click.option("--verbose", "-v", is_flag=True, help="Verbose output.")
        def status() -> None:
            """Show status."""

        @cli.group()
        def agents() -> None:
            """Agent management."""

        @agents.command()
        def list_agents() -> None:
            """List available agents."""

        return cli

    def test_returns_dict(self, sample_group: click.Group) -> None:
        pages = generate_all_man_pages(sample_group)
        assert isinstance(pages, dict)

    def test_includes_top_level(self, sample_group: click.Group) -> None:
        pages = generate_all_man_pages(sample_group)
        assert "bernstein" in pages

    def test_includes_subcommands(self, sample_group: click.Group) -> None:
        pages = generate_all_man_pages(sample_group)
        assert "status" in pages
        assert "agents" in pages

    def test_includes_nested_subcommands(self, sample_group: click.Group) -> None:
        pages = generate_all_man_pages(sample_group)
        assert "agents list-agents" in pages

    def test_all_pages_are_valid_troff(self, sample_group: click.Group) -> None:
        pages = generate_all_man_pages(sample_group)
        for name, content in pages.items():
            assert content.startswith(".TH "), f"Page {name} missing .TH header"
            assert ".SH NAME" in content, f"Page {name} missing NAME section"

    def test_options_extracted_from_click(self, sample_group: click.Group) -> None:
        pages = generate_all_man_pages(sample_group)
        status_page = pages["status"]
        assert "\\-\\-verbose" in status_page or "\\-v" in status_page


class TestWriteManPages:
    """Tests for write_man_pages()."""

    def test_writes_files(self, tmp_path: Path) -> None:
        pages = {
            "bernstein": ".TH BERNSTEIN 1\n",
            "run": ".TH BERNSTEIN-RUN 1\n",
        }
        written = write_man_pages(tmp_path / "man", pages)
        assert len(written) == 2
        assert (tmp_path / "man" / "bernstein.1").exists()
        assert (tmp_path / "man" / "bernstein-run.1").exists()

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        pages = {"run": ".TH BERNSTEIN-RUN 1\n"}
        out_dir = tmp_path / "deep" / "nested" / "man"
        write_man_pages(out_dir, pages)
        assert out_dir.exists()

    def test_file_contents_match(self, tmp_path: Path) -> None:
        content = ".TH BERNSTEIN-STATUS 1\n.SH NAME\n"
        pages = {"status": content}
        write_man_pages(tmp_path, pages)
        assert (tmp_path / "bernstein-status.1").read_text() == content

    def test_returns_sorted_paths(self, tmp_path: Path) -> None:
        pages = {"zebra": ".TH Z 1\n", "alpha": ".TH A 1\n"}
        written = write_man_pages(tmp_path, pages)
        names = [p.name for p in written]
        assert names == sorted(names)

    def test_space_in_name_becomes_hyphen(self, tmp_path: Path) -> None:
        pages = {"agents list": ".TH AGENTS-LIST 1\n"}
        written = write_man_pages(tmp_path, pages)
        assert written[0].name == "bernstein-agents-list.1"
