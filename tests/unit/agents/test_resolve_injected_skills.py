"""Tests for _resolve_and_stamp_injected_skills (issue #3382, resume path).

ASSUMPTIONS - adjust if they don't match:
  - The method lives on the same class as spawn_for_resume /
    _resolve_and_stamp_context_files in
    `bernstein.core.agents.spawner_core`. Import the class name that
    actually holds it (likely `AgentSpawner`) instead of instantiating
    the whole spawner if that's heavy to construct in a unit test - a
    bare object with the method bound via `types.MethodType` also works
    if the method only touches `session`/`tasks` and nothing else on
    `self` (check the final implementation for that).
  - `AgentSession` and `Task` are importable from their usual locations.
"""

import copy

from bernstein.core.models import AgentSession, Task

from bernstein.core.agents.spawner_core import AgentSpawner


def _make_task(task_id: str, injected_skills=None) -> Task:
    task = Task(id=task_id, title="t", description="d", role="backend")
    if injected_skills is not None:
        task.metadata["injected_skills"] = injected_skills
    return task


ORIGINAL_RECORD = [
    {
        "template_name": "completion.md",
        "version": "1.0.0",
        "pre_render_digest": "abc123",
        "rendered_digest": "def456",
        "trigger_source": "role-binding",
        "source": "injected",
        "status": "injected",
    }
]


def test_carries_forward_metadata_with_resume_preserved_source():
    task = _make_task("task-1", injected_skills=copy.deepcopy(ORIGINAL_RECORD))
    session = AgentSession(id="sess-resume-1", role="backend")

    spawner = AgentSpawner.__new__(AgentSpawner)  # bypass __init__ if heavy
    spawner._resolve_and_stamp_injected_skills(session, [task])

    assert len(session.injected_skills) == 1
    record = session.injected_skills[0]
    assert record["source"] == "resume-preserved"
    assert record["template_name"] == "completion.md"
    assert record["rendered_digest"] == "def456"
    # Original untouched
    assert ORIGINAL_RECORD[0]["source"] == "injected"


def test_falls_back_to_explicit_unknown_provenance_when_metadata_missing():
    task = _make_task("task-2")  # no injected_skills in metadata
    session = AgentSession(id="sess-resume-2", role="backend")

    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._resolve_and_stamp_injected_skills(session, [task])

    assert len(session.injected_skills) == 1
    record = session.injected_skills[0]
    assert record["status"] == "unknown_provenance"
    assert record["source"] == "resume-preserved"


def test_no_aliasing_between_session_and_task_metadata():
    task = _make_task("task-3", injected_skills=copy.deepcopy(ORIGINAL_RECORD))
    session = AgentSession(id="sess-resume-3", role="backend")

    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._resolve_and_stamp_injected_skills(session, [task])

    # Mutate the session's copy...
    session.injected_skills[0]["status"] = "mutated"

    # ...and confirm the task's own metadata copy is unaffected.
    assert task.metadata["injected_skills"][0]["status"] != "mutated"


def test_empty_tasks_list_does_not_crash():
    session = AgentSession(id="sess-resume-4", role="backend")
    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._resolve_and_stamp_injected_skills(session, [])
    assert session.injected_skills[0]["status"] == "unknown_provenance"
