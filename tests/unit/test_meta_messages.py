"""Unit tests for orchestrator meta-messages (nudges)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.models import AgentSession, Task
from bernstein.core.spawn_prompt import _render_prompt as render_v1
from bernstein.core.spawner import _render_prompt as render_v2


def test_meta_messages_in_models():
    task = Task(id="T1", title="t", description="d", role="r", meta_messages=["nudge 1"])
    assert task.meta_messages == ["nudge 1"]

    session = AgentSession(id="A1", role="r", meta_messages=["nudge 2"])
    assert session.meta_messages == ["nudge 2"]

def test_spawn_prompt_renders_meta_messages(tmp_path: Path):
    tasks = [Task(id="T1", title="t", description="d", role="backend")]
    meta = ["Policy reminder: follow PEP 8", "Phase hint: focus on architecture"]

    prompt = render_v1(
        tasks=tasks,
        templates_dir=tmp_path,
        workdir=tmp_path,
        meta_messages=meta
    )

    assert "## Operational nudges" in prompt
    assert "- Policy reminder: follow PEP 8" in prompt
    assert "- Phase hint: focus on architecture" in prompt

def test_spawner_renders_meta_messages(tmp_path: Path):
    tasks = [Task(id="T1", title="t", description="d", role="backend")]
    meta = ["Spawner nudge"]

    prompt = render_v2(
        tasks=tasks,
        templates_dir=tmp_path,
        workdir=tmp_path,
        meta_messages=meta
    )

    assert "## Operational nudges" in prompt
    assert "- Spawner nudge" in prompt

def test_no_meta_messages_no_section(tmp_path: Path):
    tasks = [Task(id="T1", title="t", description="d", role="backend")]

    prompt = render_v1(
        tasks=tasks,
        templates_dir=tmp_path,
        workdir=tmp_path,
        meta_messages=None
    )

    assert "## Operational nudges" not in prompt
