"""The QA role prompt does not name one ecosystem's tools as if they were universal (#6138).

``templates/roles/qa`` used to tell every QA agent to run ``uv run ruff check src/``.
In a repository with no Python and no ``ruff``, each agent followed that, the spawn
failed (``Failed to spawn: ruff``), and the task was retried to the same end: on one
TypeScript/Bun run, 13 of 14 task failures. The agents were doing what the prompt said.

A Python tool may still be named, because Bernstein orchestrates its own repository
too, but only behind a condition the agent can check.
"""

from __future__ import annotations

import re
from pathlib import Path

from bernstein.templates.renderer import render_role_prompt

_ROLES_DIR = Path(__file__).resolve().parents[2] / "templates" / "roles"
_QA_DIR = _ROLES_DIR / "qa"

#: Names that only exist in a Python / Bernstein checkout.
_PYTHON_TOOL = re.compile(r"\bruff\b|\buv run\b|\bpytest\b|scripts/run_tests\.py")
#: A word that makes an instruction depend on what the repository actually has.
_CONDITION = re.compile(r"\b(?:if|where|when|only)\b", re.IGNORECASE)


def _unconditional_tool_lines(text: str) -> list[str]:
    """Return the lines that name a Python tool without saying when it applies."""
    return [line.strip() for line in text.splitlines() if _PYTHON_TOOL.search(line) and not _CONDITION.search(line)]


def _system_prompt() -> str:
    return render_role_prompt("qa", {"TASK_DESCRIPTION": "check the change"}, templates_dir=_ROLES_DIR)


def test_system_prompt_names_no_python_tool_unconditionally() -> None:
    assert _unconditional_tool_lines(_system_prompt()) == []


def test_task_prompt_names_no_python_tool_unconditionally() -> None:
    """The per-task prompt repeated the same instruction as the system prompt."""
    assert _unconditional_tool_lines((_QA_DIR / "task_prompt.md").read_text(encoding="utf-8")) == []


def test_the_reported_instruction_is_gone() -> None:
    prompt = _system_prompt()

    assert "Ruff for linting and formatting: `uv run ruff check src/`" not in prompt
    assert "Project conventions (Bernstein)" not in prompt


def test_the_prompt_tells_the_agent_to_use_the_repository_own_tools() -> None:
    prompt = _system_prompt()

    assert "Use the tools this repository uses" in prompt
    assert "Do not run a tool the repository does not use" in prompt


def test_the_whole_suite_guard_survives_for_repositories_that_have_the_runner() -> None:
    """Running pytest over the whole suite exhausts memory in this repository; keep the warning."""
    prompt = _system_prompt()

    assert "scripts/run_tests.py" in prompt
    assert "never run `uv run pytest tests/` over the whole suite" in prompt
