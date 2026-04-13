"""Focused tests for spawn_prompt.py."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from bernstein.core.spawn_prompt import (
    _DIR_CACHE,
    _FILE_CACHE,
    _LESSON_CACHE_TTL,
    _extract_tags_from_tasks,
    _lesson_cache,
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
    _lesson_cache.clear()
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
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", side_effect=TemplateError("missing")),
        patch(
            "bernstein.core.spawn_prompt.gather_lessons_for_context", return_value="## Lessons\nPrefer exact parsing."
        ),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend", "qa"]),
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
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Do something", description="Description.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
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


# ---------------------------------------------------------------------------
# CRITICAL-005: Lesson cache, empty section filtering, prompt logging
# ---------------------------------------------------------------------------


def test_lesson_cache_reuses_result_within_ttl(tmp_path: Path, make_task: Any) -> None:
    """Second _render_prompt call for the same role reuses cached lessons instead of re-extracting."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build parser", description="Parse things.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    call_count = 0
    original_text = "## Lessons\nCached lesson text."

    def _counting_gather(*args: Any, **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        return original_text

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", side_effect=_counting_gather),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt1 = _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path)
        prompt2 = _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path)

    assert call_count == 1, f"gather_lessons_for_context called {call_count} times, expected 1 (cache miss + hit)"
    assert "Cached lesson text." in prompt1
    assert "Cached lesson text." in prompt2


def test_lesson_cache_expires_after_ttl(tmp_path: Path, make_task: Any) -> None:
    """Lesson cache entry is refreshed when TTL expires."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="qa", title="Run tests", description="Test stuff.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    call_count = 0

    def _counting_gather(*args: Any, **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        return f"## Lessons\nVersion {call_count}."

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a qa specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", side_effect=_counting_gather),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["qa"]),
    ):
        # First call: cache miss
        _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path)
        assert call_count == 1

        # Expire the cache by backdating the timestamp
        for key in _lesson_cache:
            ts, text = _lesson_cache[key]
            _lesson_cache[key] = (ts - _LESSON_CACHE_TTL - 1, text)

        # Second call: cache expired, should re-extract
        _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path)
        assert call_count == 2


def test_empty_sections_are_stripped(tmp_path: Path, make_task: Any) -> None:
    """Sections with empty or whitespace-only content are excluded from the final prompt."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="A-1",
            bulletin_summary="",  # empty bulletin
        )

    # Empty lessons and empty bulletin should NOT appear
    assert "## Lessons" not in prompt
    assert "Team awareness" not in prompt


def test_whitespace_bulletin_is_skipped(tmp_path: Path, make_task: Any) -> None:
    """Bulletin with only whitespace is treated as empty and excluded."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            bulletin_summary="   \n  \t  ",  # whitespace-only
        )

    assert "Team awareness" not in prompt


def test_prompt_stats_are_logged(tmp_path: Path, make_task: Any, caplog: Any) -> None:
    """_render_prompt logs character count and section count."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
        caplog.at_level(logging.INFO, logger="bernstein.core.agents.spawn_prompt"),
    ):
        _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path, session_id="A-1")

    # Check that the prompt stats log line was emitted
    stats_messages = [r.message for r in caplog.records if "Prompt for" in r.message]
    assert len(stats_messages) >= 1, f"Expected 'Prompt for' log message, got: {[r.message for r in caplog.records]}"
    msg = stats_messages[0]
    assert "backend" in msg
    assert "chars" in msg
    assert "sections" in msg


# ---------------------------------------------------------------------------
# Bidirectional bulletin board: team coordination + file ownership warnings
# ---------------------------------------------------------------------------


def test_team_coordination_section_included_with_session_id(tmp_path: Path, make_task: Any) -> None:
    """When session_id is provided, prompt includes team coordination curl instructions."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="backend-abc123",
        )

    assert "## Team coordination" in prompt
    assert "POST http://127.0.0.1:8052/bulletin" in prompt
    assert "backend-abc123" in prompt
    assert '"type": "finding"' in prompt


def test_team_coordination_section_absent_without_session_id(tmp_path: Path, make_task: Any) -> None:
    """Without session_id, team coordination section is not included."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="",
        )

    assert "## Team coordination" not in prompt


def test_file_ownership_warnings_show_other_agents_files(tmp_path: Path, make_task: Any) -> None:
    """File ownership section lists files owned by other agents."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    ownership = {
        "src/bernstein/core/orchestrator.py": "backend-abc123",
        "src/bernstein/core/models.py": "architect-def456",
        "src/bernstein/core/task_store.py": "backend-me001",
    }

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="backend-me001",
            file_ownership=ownership,
        )

    assert "## Files currently being edited by other agents" in prompt
    assert "src/bernstein/core/orchestrator.py (by backend-abc123)" in prompt
    assert "src/bernstein/core/models.py (by architect-def456)" in prompt
    # Current agent's own files should NOT be listed
    assert "src/bernstein/core/task_store.py (by backend-me001)" not in prompt


def test_file_ownership_empty_when_all_owned_by_self(tmp_path: Path, make_task: Any) -> None:
    """When all files are owned by the current agent, no ownership section appears."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    ownership = {"src/main.py": "backend-me001"}

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="backend-me001",
            file_ownership=ownership,
        )

    assert "## Files currently being edited" not in prompt


def test_file_ownership_none_produces_no_section(tmp_path: Path, make_task: Any) -> None:
    """When file_ownership is None, no ownership section appears."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build it", description="Do it.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="backend-me001",
            file_ownership=None,
        )

    assert "## Files currently being edited" not in prompt


# ---------------------------------------------------------------------------
# AGENT-012: Parent context inheritance
# ---------------------------------------------------------------------------


def test_render_prompt_includes_parent_context_when_set(tmp_path: Path, make_task: Any) -> None:
    """_render_prompt injects the parent_context section when a task carries it."""
    _lesson_cache.clear()
    task = make_task(id="sub-1", role="backend", title="Implement auth parser", description="Parse tokens.")
    task.parent_context = (
        "- **Parent goal**: Add authentication to the API\n"
        "- **Files in scope**: src/auth.py, src/models.py\n"
        "- **Parent progress**:\n  - Designed the JWT schema\n"
    )
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path)

    assert "## Parent context (inherited)" in prompt
    assert "Parent goal" in prompt
    assert "src/auth.py" in prompt
    assert "Designed the JWT schema" in prompt


def test_render_prompt_omits_parent_context_when_not_set(tmp_path: Path, make_task: Any) -> None:
    """_render_prompt omits the parent context section when parent_context is None."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Standalone task", description="No parent.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt([task], templates_dir=templates_dir, workdir=tmp_path)

    assert "## Parent context (inherited)" not in prompt


def test_render_prompt_merges_parent_context_from_multiple_tasks(tmp_path: Path, make_task: Any) -> None:
    """When a batch of tasks all carry parent_context, each is included once."""
    _lesson_cache.clear()
    task_a = make_task(id="sub-1", role="backend", title="Task A", description="A.")
    task_a.parent_context = "- Parent context for A"
    task_b = make_task(id="sub-2", role="backend", title="Task B", description="B.")
    task_b.parent_context = "- Parent context for B"
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.agents.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.agents.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.agents.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt([task_a, task_b], templates_dir=templates_dir, workdir=tmp_path)

    assert "Parent context for A" in prompt
    assert "Parent context for B" in prompt
