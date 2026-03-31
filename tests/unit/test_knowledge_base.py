"""Unit tests for knowledge-base indexing and context helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bernstein.core.knowledge_base as knowledge_base
from bernstein.core.knowledge_base import (
    TaskContextBuilder,
    _parse_python_file,
    append_decision,
    build_file_index,
    refresh_knowledge_base,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_parse_python_file_extracts_structure(tmp_path: Path) -> None:
    source = tmp_path / "src" / "demo.py"
    _write(
        source,
        '"""Demo module."""\n\n'
        "import os\n"
        "from pathlib import Path\n\n"
        "def helper() -> None:\n"
        "    return None\n\n"
        "class Service:\n"
        "    def run(self) -> None:\n"
        "        return None\n"
        "    def _private(self) -> None:\n"
        "        return None\n",
    )

    summary = _parse_python_file(source)

    assert summary is not None
    assert summary.docstring == "Demo module."
    assert summary.functions == ["helper"]
    assert summary.classes == [("Service", ["run"])]
    assert summary.imports == ["os", "pathlib"]


def test_task_context_builder_includes_subsystem_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "src" / "demo.py", '"""Demo module."""\n\ndef helper() -> None:\n    return None\n')

    def _fake_find_importers(_rel: str, _workdir: Path) -> list[str]:
        return ["tests/test_demo.py"]

    def _fake_cochanges(_rel: str, _workdir: Path, max_results: int = 3) -> list[str]:
        assert max_results == 3
        return ["src/other.py"]

    def _fake_recent_changes(_files: list[str], _workdir: Path, max_entries: int = 2) -> list[str]:
        assert max_entries == 2
        return ["feat: demo"]

    monkeypatch.setattr(knowledge_base, "_find_importers", _fake_find_importers)
    monkeypatch.setattr(knowledge_base, "_git_cochanged_files", _fake_cochanges)
    monkeypatch.setattr(knowledge_base, "_recent_git_changes", _fake_recent_changes)

    builder = TaskContextBuilder(tmp_path)
    context = builder.file_context("src/demo.py")

    assert "### src/demo.py" in context
    assert "**Imported by**: tests/test_demo.py" in context
    assert "**Often changes with**: src/other.py" in context
    assert "**Recent changes**: feat: demo" in context


def test_refresh_knowledge_base_writes_index_and_architecture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "src" / "demo.py", '"""Demo module."""\n\ndef helper() -> None:\n    return None\n')

    def _fake_ls_files_pattern(_workdir: Path, _pattern: str) -> list[str]:
        return ["src/demo.py"]

    monkeypatch.setattr(knowledge_base, "_gc_ls_files_pattern", _fake_ls_files_pattern)

    refresh_knowledge_base(tmp_path)

    index_path = tmp_path / ".sdd" / "knowledge" / "file_index.json"
    arch_path = tmp_path / ".sdd" / "knowledge" / "architecture.md"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    architecture = arch_path.read_text(encoding="utf-8")
    assert index["src/demo.py"]["docstring"] == "Demo module."
    assert "## src" in architecture
    assert "**demo.py**: Demo module." in architecture


def test_build_file_index_returns_empty_when_no_python_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _fake_ls_files_pattern(_workdir: Path, _pattern: str) -> list[str]:
        return []

    monkeypatch.setattr(knowledge_base, "_gc_ls_files_pattern", _fake_ls_files_pattern)

    index = build_file_index(tmp_path)

    assert index == {}


def test_append_decision_keeps_only_last_15_entries(tmp_path: Path) -> None:
    decisions_path = tmp_path / ".sdd" / "knowledge" / "recent_decisions.md"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ["# Recent Decisions\n"]
    for idx in range(20):
        existing.append(f"\n## [2026-03-31 10:{idx:02d}] Existing {idx} (T-{idx})\nsummary\n")
    decisions_path.write_text("".join(existing), encoding="utf-8")

    append_decision(tmp_path, "T-new", "Newest", "latest summary")

    content = decisions_path.read_text(encoding="utf-8")
    assert content.count("## [") == 15
    assert "Newest (T-new)" in content
