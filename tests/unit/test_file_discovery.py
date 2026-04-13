"""Focused tests for file_discovery.py."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bernstein.core.file_discovery import (
    clear_caches,
    file_tree,
    gather_project_context,
    gather_project_memory,
    get_recent_project_memory,
)


def test_file_tree_uses_git_ls_files_and_caches_results(tmp_path: Path) -> None:
    """file_tree prefers git ls-files output and reuses the cached result within the TTL."""
    clear_caches()
    with patch(
        "bernstein.core.knowledge.file_discovery._gc_ls_files", return_value=["src/a.py", "src/b.py"]
    ) as mock_ls:
        first = file_tree(tmp_path)
        second = file_tree(tmp_path)

    assert first == "src/a.py\nsrc/b.py"
    assert second == first
    mock_ls.assert_called_once_with(tmp_path)


def test_file_tree_falls_back_to_walk_and_skips_ignored_paths(tmp_path: Path) -> None:
    """file_tree falls back to recursive walk and omits ignored directories and suffixes."""
    clear_caches()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cache.pyc").write_text("", encoding="utf-8")

    with patch("bernstein.core.knowledge.file_discovery._gc_ls_files", return_value=[]):
        tree = file_tree(tmp_path)

    assert "src/main.py" in tree
    assert ".git/config" not in tree
    assert "cache.pyc" not in tree


def test_gather_project_context_includes_tree_readme_and_project_md(tmp_path: Path) -> None:
    """gather_project_context assembles the file tree, README, and .sdd project description sections."""
    (tmp_path / "README.md").write_text("Repo readme", encoding="utf-8")
    project_md = tmp_path / ".sdd" / "project.md"
    project_md.parent.mkdir(parents=True)
    project_md.write_text("Project notes", encoding="utf-8")

    with patch("bernstein.core.knowledge.file_discovery.file_tree", return_value="src/main.py"):
        context = gather_project_context(tmp_path)

    assert "## File tree" in context
    assert "Repo readme" in context
    assert "Project notes" in context


def test_get_recent_project_memory_reads_latest_markdown_entries(tmp_path: Path) -> None:
    """get_recent_project_memory reads recent knowledge markdown entries into structured dicts."""
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    first = kb_dir / "2026-03-01.md"
    second = kb_dir / "2026-03-02.md"
    first.write_text("# First\nSummary one", encoding="utf-8")
    second.write_text("# Second\nSummary two", encoding="utf-8")

    items = get_recent_project_memory(tmp_path, limit=2)

    assert [item["title"] for item in items] == ["Second", "First"]


def test_gather_project_memory_formats_recent_decisions(tmp_path: Path) -> None:
    """gather_project_memory formats recent project decisions into a markdown summary."""
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "2026-03-01.md").write_text("# Decision A\nUse exact parsing", encoding="utf-8")

    summary = gather_project_memory(tmp_path)

    assert "## Recent project decisions" in summary
    assert "Decision A" in summary
