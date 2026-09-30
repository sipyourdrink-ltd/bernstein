"""Templates must not hardcode the task-server URL (#6299).

Agents on remote workers or non-default ports reach the task server at a
URL the spawner resolves at runtime, so prompt templates use the
``{{SERVER_URL}}`` placeholder instead of ``http://127.0.0.1:8052``.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.agents.spawn_prompt import _render_completion_instructions
from bernstein.core.tasks.models import Task
from bernstein.templates.renderer import render_template

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_LITERAL = "127.0.0.1:8052"
# Deployment configs and an MCP server default legitimately name the port.
_ALLOWED = {
    "cloudflare-mcp-server/src/index.ts",
    "cloudflare-mcp-server/wrangler.toml",
    "gitlab-ci.yml",
}


def test_no_literal_server_url_in_prompt_templates() -> None:
    offenders = []
    for path in sorted(_TEMPLATES.rglob("*")):
        rel = path.relative_to(_TEMPLATES).as_posix()
        if not path.is_file() or rel in _ALLOWED or "node_modules" in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _LITERAL in text:
            offenders.append(rel)
    assert not offenders, f"hardcoded {_LITERAL} in: {offenders}"


def test_role_task_prompt_substitutes_server_url() -> None:
    path = _TEMPLATES / "roles" / "backend" / "task_prompt.md"
    out = render_template(path, {"TASK_ID": "T-1", "SERVER_URL": "http://10.0.0.5:9000"})
    assert "http://10.0.0.5:9000/tasks/T-1/fail" in out
    assert "{{SERVER_URL}}" not in out


def test_completion_instructions_use_resolved_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://10.0.0.5:9000/")
    task = Task(id="T-1", title="t", description="d", role="backend")
    out = _render_completion_instructions([task], tmp_path)
    assert "http://10.0.0.5:9000/tasks/T-1/complete" in out
    assert "{{SERVER_URL}}" not in out
