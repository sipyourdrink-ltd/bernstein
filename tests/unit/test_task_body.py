"""Class-level guard: every TaskCreate field is forwarded or deliberately not."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from bernstein.core.models import CompletionSignal, Task

from bernstein.core.orchestration import manager
from bernstein.core.planning import planner
from bernstein.core.quality.janitor import evaluate_signal
from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.backlog_parser import ParsedBacklogTask

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def test_every_task_create_field_is_classified() -> None:
    from bernstein.core.tasks.task_body import FORWARDED_FIELDS, NOT_FORWARDED

    fields = set(TaskCreate.model_fields)
    unclassified = fields - set(FORWARDED_FIELDS) - set(NOT_FORWARDED)
    assert not unclassified, f"decide forward-or-not (with a reason) for: {sorted(unclassified)}"
    assert not set(FORWARDED_FIELDS) & set(NOT_FORWARDED)
    assert not (set(FORWARDED_FIELDS) | set(NOT_FORWARDED)) - fields, "stale entry"
    assert all(NOT_FORWARDED.values()), "every non-forwarded field needs a reason"


def test_body_validates_as_task_create_and_carries_signals() -> None:
    from bernstein.core.tasks.task_body import build_task_body

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


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", [planner, manager], ids=["planner", "manager"])
async def test_declared_witnesses_and_context_survive_task_creation(tmp_path: Path, producer: ModuleType) -> None:
    task = Task(
        id="t1",
        title="Check output",
        description="Keep the declared completion witness",
        role="backend",
        completion_signals=[CompletionSignal(type="file_contains", value="result.txt :: accepted")],
        metadata={"context_files": ["contract.md"], "loader_internal": "not forwarded"},
    )
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        task_id = await producer._post_task_to_server(client, "http://server", task)
    stored = app.state.store.get_task(task_id)
    assert stored is not None
    assert [(signal.type, signal.value) for signal in stored.completion_signals] == [
        ("file_contains", "result.txt :: accepted")
    ]
    assert stored.metadata == {"context_files": ["contract.md"]}
    (tmp_path / "result.txt").write_text("rejected", encoding="utf-8")
    assert not evaluate_signal(stored.completion_signals[0], tmp_path)[0]
    (tmp_path / "result.txt").write_text("accepted", encoding="utf-8")
    assert evaluate_signal(stored.completion_signals[0], tmp_path)[0]


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
