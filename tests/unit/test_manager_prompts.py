"""Focused tests for manager prompt rendering helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from bernstein.core.manager_prompts import (
    _format_existing_tasks,
    _format_roles,
    _load_template,
    render_plan_prompt,
    render_queue_review_prompt,
    render_review_prompt,
)


def test_load_template_prefers_versioned_prompt_when_available(tmp_path: Path) -> None:
    """_load_template returns versioned prompt content when the prompt registry supplies one."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    class _Version:
        def __init__(self) -> None:
            self.content = "Versioned prompt"

    class _Registry:
        def __init__(self, sdd_dir: Path) -> None:
            del sdd_dir

        def select_version(self, stem: str, task_id: str = "") -> int:
            del stem, task_id
            return 2

        def get_version(self, stem: str, version: int) -> _Version:
            del stem, version
            return _Version()

    with patch("bernstein.core.tokens.prompt_versioning.PromptRegistry", _Registry):
        content = _load_template(templates_dir, "plan.md", sdd_dir=tmp_path / ".sdd", task_id="T-1")

    assert content == "Versioned prompt"


def test_format_roles_and_existing_tasks_render_human_readable_lists(make_task: Any) -> None:
    """_format_roles and _format_existing_tasks produce compact markdown summaries."""
    task = make_task(id="T-1", title="Fix auth", role="backend")

    assert _format_roles(["backend", "qa"]) == "- backend\n- qa"
    assert "[open] Fix auth (role=backend)" in _format_existing_tasks([task])


def test_render_plan_prompt_replaces_all_template_placeholders(tmp_path: Path, make_task: Any) -> None:
    """render_plan_prompt interpolates goal, context, roles, and existing tasks into the template."""
    templates_dir = tmp_path / "templates"
    prompts_dir = templates_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "plan.md").write_text(
        "Goal={{GOAL}}\nContext={{CONTEXT}}\nRoles={{AVAILABLE_ROLES}}\nTasks={{EXISTING_TASKS}}",
        encoding="utf-8",
    )
    task = make_task(id="T-1", title="Fix auth", role="backend")

    prompt = render_plan_prompt("Ship auth", "Repo context", ["backend"], [task], templates_dir)

    assert "Goal=Ship auth" in prompt
    assert "Context=Repo context" in prompt
    assert "Roles=- backend" in prompt
    assert "Fix auth" in prompt


def test_render_queue_review_prompt_lists_open_claimed_and_failed_tasks(make_task: Any) -> None:
    """render_queue_review_prompt enumerates all queue buckets and response rules."""
    open_task = make_task(id="T-open", title="Plan", role="manager")
    claimed_task = make_task(id="T-claimed", title="Build", role="backend")
    failed_task = make_task(id="T-failed", title="Test", role="qa")

    prompt = render_queue_review_prompt(3, 1, [open_task], [claimed_task], [failed_task], "http://server")

    assert "3 task(s) completed, 1 failed" in prompt
    assert "### Open (waiting):" in prompt
    assert "### In progress:" in prompt
    assert "### Recently failed:" in prompt


def test_render_review_prompt_includes_completion_signals_and_summary(tmp_path: Path, make_task: Any) -> None:
    """render_review_prompt renders completion signals and result summary into the review template."""
    templates_dir = tmp_path / "templates"
    prompts_dir = templates_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "review.md").write_text(
        "Title={{TASK_TITLE}}\nSignals={{COMPLETION_SIGNALS}}\nSummary={{RESULT_SUMMARY}}\nContext={{CONTEXT}}",
        encoding="utf-8",
    )
    task = make_task(id="T-1", title="Fix auth", description="Desc")
    task.result_summary = "Done"

    prompt = render_review_prompt(task, "Repo context", templates_dir)

    assert "Title=Fix auth" in prompt
    assert "Summary=Done" in prompt
    assert "Context=Repo context" in prompt
