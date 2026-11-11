"""Agent-facing templates must use the task server URL supplied by the run."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bernstein.core.skills.load_skill_tool import load_skill
from bernstein.core.skills.loader import SkillLoader
from bernstein.core.skills.sources import LocalDirSkillSource
from bernstein.templates.renderer import render_template

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_ROLE_PROMPTS = sorted((_TEMPLATES / "roles").glob("*/task_prompt.md"))
_SERVER_URLS = ("http://127.0.0.1:8099", "http://10.0.0.7:9001")
_BASE_URL_RE = re.compile(r"http://(?:127\.0\.0\.1|localhost|10\.0\.0\.7):\d+")


@pytest.mark.parametrize("server_url", _SERVER_URLS)
@pytest.mark.parametrize("template", _ROLE_PROMPTS, ids=lambda path: path.parent.name)
def test_role_task_prompt_uses_one_server(template: Path, server_url: str) -> None:
    prompt = render_template(
        template,
        {
            "SERVER_URL": server_url,
            "TASK_ID": "T-7",
            "TASK_TITLE": "Sample task",
            "TASK_DESCRIPTION": "Sample body",
            "AGENT_ID": "agent-7",
        },
    )

    assert "8052" not in prompt
    assert "{{SERVER_URL}}" not in prompt
    assert set(_BASE_URL_RE.findall(prompt)) == {server_url}
    assert f"{server_url}/tasks/T-7/complete" in prompt
    if "/fail" in prompt:
        assert f"{server_url}/tasks/T-7/fail" in prompt
    if "/bulletin" in prompt:
        assert f"{server_url}/bulletin" in prompt


@pytest.mark.parametrize("server_url", _SERVER_URLS)
@pytest.mark.parametrize("name", ("progress-report.md", "team-awareness.md"))
def test_prompt_partial_uses_template_context(name: str, server_url: str) -> None:
    prompt = render_template(
        _TEMPLATES / "prompts" / name,
        {
            "SERVER_URL": server_url,
            "TASK_ID": "T-7",
            "SESSION_ID": "session-7",
            "AGENT_ID": "agent-7",
            "BULLETIN_SUMMARY": "Created shared utility",
        },
    )

    assert "8052" not in prompt
    assert "{{SERVER_URL}}" not in prompt
    assert set(_BASE_URL_RE.findall(prompt)) == {server_url}
    if name == "progress-report.md":
        assert f"{server_url}/tasks/T-7/progress" in prompt
        assert ".sdd/runtime/signals/session-7/WAKEUP" in prompt
    else:
        assert "Created shared utility" in prompt
        assert f"{server_url}/bulletin" in prompt
        assert '"agent_id": "agent-7"' in prompt


def test_manager_task_api_reference_uses_server_from_system_prompt() -> None:
    loader = SkillLoader([LocalDirSkillSource(_TEMPLATES / "skills")])
    result = load_skill(name="manager", reference="task-api.md", loader=loader)

    assert result.error is None
    reference = result.reference_content
    assert reference is not None
    assert "8052" not in reference
    assert "{{SERVER_URL}}" not in reference
    assert "system prompt" in reference
    assert "SERVER_URL" in reference
    assert reference.count('"${SERVER_URL}/tasks"') == 2
