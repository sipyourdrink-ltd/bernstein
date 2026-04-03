"""Focused tests for spawn_prompt.py."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from bernstein.core.spawn_prompt import (
    _DIR_CACHE,
    _FILE_CACHE,
    _extract_tags_from_tasks,
    _list_subdirs_cached,
    _read_cached,
    _render_predecessor_context,
    _render_prompt,
)
from bernstein.templates.renderer import TemplateError


def test_read_cached_reuses_cached_content_until_file_mtime_changes(tmp_path: Path) -> None:
    """_read_cached returns cached content until the underlying file mtime changes."""
    _FILE_CACHE.clear()
    path = tmp_path / "project.md"
    path.write_text("v1", encoding="utf-8")

    with patch("pathlib.Path.stat", side_effect=[SimpleNamespace(st_mtime=1.0), SimpleNamespace(st_mtime=1.0)]):
        first = _read_cached(path)
        path.write_text("v2", encoding="utf-8")
        second = _read_cached(path)

    with patch("pathlib.Path.stat", return_value=SimpleNamespace(st_mtime=2.0)):
        third = _read_cached(path)

    assert first == "v1"
    assert second == "v1"
    assert third == "v2"


def test_list_subdirs_cached_returns_sorted_names(tmp_path: Path) -> None:
    """_list_subdirs_cached returns sorted immediate subdirectories and caches the result."""
    _DIR_CACHE.clear()
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "file.txt").write_text("", encoding="utf-8")

    # Use real filesystem stat — no mocking needed for this test
    names = _list_subdirs_cached(tmp_path)
    assert names == ["alpha", "zeta"]


def test_extract_tags_from_tasks_filters_stop_words(make_task: Any) -> None:
    """_extract_tags_from_tasks keeps meaningful title words and the task role."""
    task = make_task(role="Backend", title="Fix the auth parser and retry flow")

    tags = _extract_tags_from_tasks([task])

    assert "backend" in tags
    assert "auth" in tags
    assert "parser" in tags
    assert "the" not in tags


def test_render_predecessor_context_formats_informs_and_transforms(make_task: Any) -> None:
    """_render_predecessor_context formats predecessor summaries with their edge semantics."""
    task = make_task(id="T-1")

    def _predecessor_context(task_id: str) -> list[dict[str, str]]:
        if task_id != "T-1":
            return []
        return [
            {"title": "Research auth", "edge_type": "informs", "result_summary": "Mapped the API."},
            {"title": "Normalize schema", "edge_type": "transforms", "result_summary": "Produced JSON schema."},
        ]

    graph = cast(Any, SimpleNamespace(predecessor_context=_predecessor_context))

    rendered = _render_predecessor_context([task], graph)

    assert "Research auth" in rendered
    assert "informed by" in rendered
    assert "transforms output of" in rendered


def test_render_prompt_falls_back_and_includes_context_sections(tmp_path: Path, make_task: Any) -> None:
    """_render_prompt falls back to a default role prompt and includes project, lessons, predecessor, and signal sections."""
    task = make_task(id="T-1", role="backend", title="Implement auth parser", description="Build the parser.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    project_md = tmp_path / ".sdd" / "project.md"
    project_md.parent.mkdir(parents=True)
    project_md.write_text("Project context here.", encoding="utf-8")

    def _predecessor_context(task_id: str) -> list[dict[str, str]]:
        del task_id
        return [{"title": "Research auth", "edge_type": "informs", "result_summary": "Use token auth."}]

    graph = cast(Any, SimpleNamespace(predecessor_context=_predecessor_context))

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", side_effect=TemplateError("missing")),
        patch(
            "bernstein.core.spawn_prompt.gather_lessons_for_context", return_value="## Lessons\nPrefer exact parsing."
        ),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["backend", "qa"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="A-1",
            bulletin_summary="- QA fixed failing auth tests.",
            task_graph=graph,
        )

    assert "You are a backend specialist." in prompt
    assert "Project context here." in prompt
    assert "Prefer exact parsing." in prompt
    assert "Research auth" in prompt
    assert "Other agents are working in parallel" in prompt
    assert "Signal files — check periodically" in prompt


def test_render_prompt_includes_git_safety_protocol(tmp_path: Path, make_task: Any) -> None:
    """_render_prompt always injects the git safety protocol section."""
    task = make_task(id="T-1", role="backend", title="Do something", description="Description.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="A-1",
        )

    assert "Git safety protocol" in prompt
    assert "--force" in prompt
    assert "no-verify" in prompt
    assert "NEVER commit secrets" in prompt


def test_render_git_safety_protocol_content() -> None:
    """_render_git_safety_protocol produces the expected safety rules."""
    from bernstein.core.spawn_prompt import _render_git_safety_protocol

    safety = _render_git_safety_protocol()
    assert "Git safety protocol" in safety
    assert "--force" in safety
    assert "no-verify" in safety
    assert "secrets" in safety
    assert "git diff" in safety
    assert "agent/" in safety
