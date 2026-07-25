"""End-to-end CLI tests for ``bernstein wiki build``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from bernstein.cli.wiki_cmd import wiki_group
from click.testing import CliRunner


def _init_git_repo(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--quiet",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Materialise a tiny git-tracked Python package for CLI tests."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "service.py").write_text(
        '"""Service."""\n\ndef public_api() -> int:\n    """Public entry point."""\n    return 1\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(
        "def test_ok() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _init_git_repo(tmp_path)
    return tmp_path


def test_wiki_build_streams_markdown_to_stdout(fixture_repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["build", "--repo", str(fixture_repo)])

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"# {fixture_repo.name} - Repo Wiki\n")
    assert "public_api" in result.output
    assert "## Test layout" in result.output


def test_wiki_build_writes_file_with_flag(fixture_repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        wiki_group,
        ["build", "--repo", str(fixture_repo), "--write"],
    )

    assert result.exit_code == 0, result.output
    written = fixture_repo / "WIKI.md"
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert content.startswith(f"# {fixture_repo.name} - Repo Wiki\n")
    assert "public_api" in content


def test_wiki_build_respects_custom_output_path(fixture_repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "out" / "DOCS.md"
    runner = CliRunner()
    result = runner.invoke(
        wiki_group,
        [
            "build",
            "--repo",
            str(fixture_repo),
            "--output",
            str(target),
        ],
    )

    assert result.exit_code == 0, result.output
    assert target.exists()
    assert "public_api" in target.read_text(encoding="utf-8")


def test_wiki_help_describes_local_operation() -> None:
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["--help"])

    assert result.exit_code == 0
    # The help text states where the command runs, so an operator can tell
    # it needs no network and no external service before invoking it.
    assert "locally" in result.output
    assert "offline" in result.output


def test_wiki_build_handles_repo_with_no_python(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _init_git_repo(tmp_path)

    runner = CliRunner()
    result = runner.invoke(wiki_group, ["build", "--repo", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Repo Wiki" in result.output
    assert "No public symbols extracted from the graph" in result.output
