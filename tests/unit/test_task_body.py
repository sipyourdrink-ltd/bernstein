"""Class-level guard: every TaskCreate field is forwarded or deliberately not."""

from __future__ import annotations

import inspect

from bernstein.core.models import CompletionSignal, Task

from bernstein.core.orchestration import manager
from bernstein.core.planning import planner
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.backlog_parser import ParsedBacklogTask
from bernstein.core.tasks.task_body import FORWARDED_FIELDS, NOT_FORWARDED, build_task_body


def test_every_task_create_field_is_classified() -> None:
    fields = set(TaskCreate.model_fields)
    unclassified = fields - set(FORWARDED_FIELDS) - set(NOT_FORWARDED)
    assert not unclassified, f"decide forward-or-not (with a reason) for: {sorted(unclassified)}"
    assert not set(FORWARDED_FIELDS) & set(NOT_FORWARDED)
    assert not (set(FORWARDED_FIELDS) | set(NOT_FORWARDED)) - fields, "stale entry"
    assert all(NOT_FORWARDED.values()), "every non-forwarded field needs a reason"


def test_body_validates_as_task_create_and_carries_signals() -> None:
    task = Task(
        id="t1",
        title="t",
        description="d",
        role="backend",
        completion_signals=[CompletionSignal(type="file_contains", value="x")],
        metadata={"context_files": ["a.py"]},
    )
    body = build_task_body(task)
    parsed = TaskCreate(**body)
    assert [(s.type, s.value) for s in parsed.completion_signals] == [("file_contains", "x")]
    assert parsed.metadata == {"context_files": ["a.py"]}


def test_call_sites_share_the_builder() -> None:
    assert "build_task_body" in inspect.getsource(planner._post_task_to_server)
    assert "build_task_body" in inspect.getsource(manager._post_task_to_server)


def test_backlog_payload_emits_janitor_signals() -> None:
    bt = ParsedBacklogTask(
        title="t",
        description="d",
        role="backend",
        priority=2,
        scope="medium",
        complexity="medium",
        source_file="b.yaml",
        janitor_signals=({"type": "path_exists", "value": "x.py"},),
    )
    payload = bt.to_task_payload()
    assert payload["completion_signals"] == [{"type": "path_exists", "value": "x.py"}]
    TaskCreate(**payload)
