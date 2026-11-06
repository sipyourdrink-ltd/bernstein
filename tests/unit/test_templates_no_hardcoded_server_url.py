"""Agent prompt sources follow the selected server's existing URL contract (#6299)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bernstein.core.prompt_versioning import PromptRegistry, seed_prompts_from_templates

from bernstein.core.agents.spawner_core import _resolve_task_server_url
from bernstein.core.defaults import SDD_SERVER_PORT
from bernstein.core.skills.load_skill_tool import load_skill
from bernstein.core.skills.loader import SkillLoader
from bernstein.core.skills.sources.local_dir import LocalDirSkillSource
from bernstein.templates.renderer import render_template

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_ROLES = (
    "adversary",
    "architect",
    "backend",
    "ci-fixer",
    "devops",
    "docs",
    "frontend",
    "manager",
    "ml-engineer",
    "qa",
    "retrieval",
    "reviewer",
    "security",
)
_PARTIALS = ("progress-report", "team-awareness")
_REFERENCE = _TEMPLATES / "skills/manager/references/task-api.md"
_URL = re.compile(r"https?://[^\s/`]+")


def test_raw_manager_reference_uses_the_prompt_server_without_rendering() -> None:
    loader = SkillLoader([LocalDirSkillSource(_TEMPLATES / "skills")])
    result = load_skill("manager", reference="task-api.md", loader=loader, wal_sink=lambda _event: None)
    assert result.error is None
    assert result.reference_content == _REFERENCE.read_text(encoding="utf-8")
    reference = result.reference_content
    assert reference is not None
    assert "system prompt" in reference
    assert "<base-url>/tasks" in reference
    assert "127.0.0.1:8052" not in reference
    assert "{{SERVER_URL}}" not in reference


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("remote", [False, True], ids=["run-port", "remote-override"])
def test_role_task_template_and_completion_include_share_the_supplied_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, remote: bool
) -> None:
    # These files have a generic renderer, but no automatic role-dispatch caller.
    port = tmp_path / SDD_SERVER_PORT
    port.parent.mkdir(parents=True)
    port.write_text("8099", encoding="utf-8")
    if remote:
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://10.0.0.5:9000/")
        expected = "http://10.0.0.5:9000"
    else:
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        expected = "http://127.0.0.1:8099"
    resolved = _resolve_task_server_url(tmp_path)
    assert resolved == expected
    rendered = render_template(
        _TEMPLATES / "roles" / role / "task_prompt.md",
        {"SERVER_URL": resolved, "TASK_ID": "T-1", "AGENT_ID": "agent-1"},
    )
    assert expected in rendered
    assert set(_URL.findall(rendered)) == {expected}
    assert "{{SERVER_URL}}" not in rendered
    assert "{{TASK_ID}}" not in rendered
    assert "{{AGENT_ID}}" not in rendered
    assert "127.0.0.1:8052" not in rendered


@pytest.mark.parametrize("name", _PARTIALS)
def test_prompt_partial_registry_seeding_preserves_parameterized_source(tmp_path: Path, name: str) -> None:
    # Seeding stores raw template material; it does not render or dispatch it.
    sdd_dir = tmp_path / ".sdd"
    assert seed_prompts_from_templates(sdd_dir, _TEMPLATES) > 0
    registry = PromptRegistry(sdd_dir)
    version = registry.get_version(name, 1)
    assert version is not None
    raw = (_TEMPLATES / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    assert version.content == raw
    assert "{{SERVER_URL}}" in version.content
    assert "127.0.0.1:8052" not in version.content
    if name == "progress-report":
        assert "{TASK_ID}" in version.content


def test_agent_prompt_sources_do_not_pin_the_default_task_port() -> None:
    sources = [_TEMPLATES / "roles" / role / "task_prompt.md" for role in _ROLES]
    sources += [_TEMPLATES / "prompts" / f"{name}.md" for name in _PARTIALS]
    sources.append(_REFERENCE)
    for source in sources:
        assert not source.read_bytes().startswith(b"\xef\xbb\xbf"), source
        assert "127.0.0.1:8052" not in source.read_text(encoding="utf-8"), source
    # Deployment defaults are intentionally outside this agent-prompt contract.
    assert "127.0.0.1:8052" in (_TEMPLATES / "gitlab-ci.yml").read_text(encoding="utf-8")
