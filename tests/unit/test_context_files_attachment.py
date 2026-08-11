"""Declared task context reaches the worker and the run record (issue #3375).

Three surfaces parsed ``context_files`` and all three dropped it before a
worker saw anything: the backlog payload, the plan-loader callers, and the
task POST body. These tests pin the reconnected wire property by property:
the declaration rides the real parser -> payload -> task metadata -> the real
spawner-side context-writing function, the resolved set is recorded with
content addresses in declared order, absence is explicit via reason codes,
the record round-trips through recomputed digests, and a task that declares
nothing behaves byte-identically to before.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bernstein.core.backlog_parser import parse_backlog_text
from bernstein.core.models import AgentSession, Task
from bernstein.core.plan_loader import load_plan
from bernstein.core.worktree_claude_md import generate_claude_md, write_claude_md

from bernstein.core.agents.context_attachments import (
    CONTEXT_FILES_ATTACHED_EVENT,
    REASON_INVALID,
    REASON_IS_DIRECTORY,
    REASON_MISSING,
    REASON_OUTSIDE_ROOT,
    collect_declared_context_files,
    resolve_context_attachments,
    verify_context_attachments,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICKET_WITH_CONTEXT = """---
title: Wire the auth retry
role: backend
context_files: ["docs/adr/0007-retries.md", "docs/api/auth.md"]
---

# Wire the auth retry
"""

_TICKET_WITHOUT_CONTEXT = """---
title: Wire the auth retry
role: backend
---

# Wire the auth retry
"""

_PLAN_WITH_CONTEXT = """
name: retry-hardening
context_files:
  - docs/adr/0007-retries.md
stages:
  - name: build
    steps:
      - title: Wire the auth retry
        role: backend
      - title: Cover the retry with tests
        role: qa
"""

_PLAN_WITHOUT_CONTEXT = """
name: retry-hardening
stages:
  - name: build
    steps:
      - title: Wire the auth retry
        role: backend
"""


def _task_from_payload(payload: dict[str, object]) -> Task:
    """Build a Task the way the task store does: ``metadata`` copied verbatim."""
    return Task(
        id="T-1",
        title=str(payload["title"]),
        description=str(payload["description"]),
        role=str(payload["role"]),
        metadata=dict(payload.get("metadata") or {}),  # type: ignore[call-overload]
    )


def _write_worker_context(worktree: Path, tasks: list[Task]) -> str:
    """Drive the real spawner-side context-writing path against *worktree*.

    Mirrors the spawn path exactly: collect declared context files off the
    task batch with the helper the spawner uses, then hand them to
    ``write_claude_md`` the way ``spawner_core`` does.
    """
    declared = collect_declared_context_files(tasks)
    write_claude_md(
        worktree,
        tasks,
        session_id="sess-1",
        role="backend",
        workdir=worktree,
        context_files=declared or None,
    )
    return (worktree / "CLAUDE.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Ticket and plan declarations reach the worker worktree
# ---------------------------------------------------------------------------


def test_ticket_declared_context_files_reach_the_worker_worktree(tmp_path: Path) -> None:
    """Real parser -> payload -> task metadata -> per-session context on disk."""
    parsed = parse_backlog_text("ticket.md", _TICKET_WITH_CONTEXT)
    assert parsed is not None
    payload = parsed.to_task_payload()
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["context_files"] == ["docs/adr/0007-retries.md", "docs/api/auth.md"]

    task = _task_from_payload(payload)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    content = _write_worker_context(worktree, [task])

    assert "## Context files" in content
    assert "`docs/adr/0007-retries.md`" in content
    assert "`docs/api/auth.md`" in content


def test_plan_declared_context_files_reach_the_worker_worktree(tmp_path: Path) -> None:
    """Plan-level context_files land on every task and reach the worktree."""
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(_PLAN_WITH_CONTEXT, encoding="utf-8")

    _config, tasks = load_plan(plan_file)

    assert len(tasks) == 2
    for task in tasks:
        assert task.metadata["context_files"] == ["docs/adr/0007-retries.md"]

    worktree = tmp_path / "wt"
    worktree.mkdir()
    content = _write_worker_context(worktree, tasks)

    assert "## Context files" in content
    assert "`docs/adr/0007-retries.md`" in content


def test_plan_task_metadata_context_files_survive_the_task_post(tmp_path: Path) -> None:
    """The POST body is built field-by-field; the declaration must be forwarded."""
    from bernstein.core.planning.planner import _post_task_to_server

    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(_PLAN_WITH_CONTEXT, encoding="utf-8")
    _config, tasks = load_plan(plan_file)

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"id": "srv-1"}

    class _FakeClient:
        def __init__(self) -> None:
            self.bodies: list[dict[str, Any]] = []

        async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
            self.bodies.append(json)
            return _FakeResponse()

    client = _FakeClient()
    asyncio.run(_post_task_to_server(client, "http://server", tasks[0]))  # type: ignore[arg-type]

    assert client.bodies[0]["metadata"] == {"context_files": ["docs/adr/0007-retries.md"]}


# ---------------------------------------------------------------------------
# Recorded attachment set: content addresses, order, reason codes
# ---------------------------------------------------------------------------


def test_each_attachment_is_recorded_with_path_sha256_and_declared_order(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_bytes(b"alpha\n")
    (tmp_path / "b.md").write_bytes(b"bravo\n")

    entries = resolve_context_attachments(root=tmp_path, declared=["docs/a.md", "b.md"])

    assert [e["path"] for e in entries] == ["docs/a.md", "b.md"]
    assert [e["order"] for e in entries] == [0, 1]
    assert entries[0]["sha256"] == "sha256:" + hashlib.sha256(b"alpha\n").hexdigest()
    assert entries[1]["sha256"] == "sha256:" + hashlib.sha256(b"bravo\n").hexdigest()
    assert all(e["reason_code"] == "" for e in entries)


def test_unresolvable_context_path_is_recorded_with_a_reason_code_not_skipped(tmp_path: Path) -> None:
    """Every declared path keeps its position; the entry count never shrinks."""
    (tmp_path / "real.md").write_bytes(b"content\n")
    (tmp_path / "adir").mkdir()

    declared = ["real.md", "no-such.md", "adir", "../escape.md"]
    entries = resolve_context_attachments(root=tmp_path, declared=declared)

    assert len(entries) == len(declared)
    assert [e["path"] for e in entries] == declared
    assert entries[0]["reason_code"] == ""
    assert entries[1]["reason_code"] == REASON_MISSING
    assert entries[2]["reason_code"] == REASON_IS_DIRECTORY
    assert entries[3]["reason_code"] == REASON_OUTSIDE_ROOT
    for entry in entries[1:]:
        assert entry["sha256"] == ""


def test_declared_but_missing_file_is_recorded_with_missing_reason_code(tmp_path: Path) -> None:
    """Absence is explicit: a missing declared file is a record, not a no-op."""
    entries = resolve_context_attachments(root=tmp_path, declared=["never-written.md"])

    assert entries == [
        {
            "path": "never-written.md",
            "order": 0,
            "sha256": "",
            "reason_code": REASON_MISSING,
        }
    ]


def test_a_malformed_declared_path_is_recorded_with_a_reason_code_not_a_crash(tmp_path: Path) -> None:
    """A path the filesystem cannot represent (embedded NUL) must not abort the spawn."""
    (tmp_path / "ok.md").write_bytes(b"fine\n")

    declared = ["ok.md", "bad\x00name.md", "gone.md"]
    entries = resolve_context_attachments(root=tmp_path, declared=declared)

    assert len(entries) == len(declared)
    assert [e["path"] for e in entries] == declared
    assert entries[0]["reason_code"] == ""
    assert entries[1] == {
        "path": "bad\x00name.md",
        "order": 1,
        "sha256": "",
        "reason_code": REASON_INVALID,
    }
    assert entries[2]["reason_code"] == REASON_MISSING

    # The verify side must not abort either: the malformed entry re-resolves
    # to the same recorded absence, so the round trip still matches.
    assert verify_context_attachments(root=tmp_path, entries=entries) == []


def test_recorded_attachment_set_round_trips_via_recomputed_digests(tmp_path: Path) -> None:
    """A verifier recomputes every digest from the bytes and matches."""
    doc = tmp_path / "doc.md"
    doc.write_bytes(b"original\n")

    entries = resolve_context_attachments(root=tmp_path, declared=["doc.md", "gone.md"])
    assert verify_context_attachments(root=tmp_path, entries=entries) == []

    doc.write_bytes(b"tampered\n")
    mismatches = verify_context_attachments(root=tmp_path, entries=entries)
    assert len(mismatches) == 1
    assert mismatches[0].startswith("doc.md:")


# ---------------------------------------------------------------------------
# Run-record wiring
# ---------------------------------------------------------------------------


def _record_spawned(session: AgentSession) -> list[tuple[str, dict[str, Any]]]:
    """Run the real ``_record_spawned_events`` body against a fake orchestrator."""
    from bernstein.core.orchestration.orchestrator import Orchestrator

    events: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def record(self, event: str, **data: Any) -> None:
            events.append((event, data))

    fake = SimpleNamespace(
        _agents={session.id: session},
        _recorder=_Recorder(),
        _record_mutation_capability_once=lambda _session: None,
    )
    result = SimpleNamespace(spawned=[session.id])
    Orchestrator._record_spawned_events(fake, result)  # type: ignore[arg-type]
    return events


def test_context_attachments_are_recorded_in_the_run_journal_next_to_agent_spawned() -> None:
    attachment = {"path": "docs/a.md", "order": 0, "sha256": "sha256:" + "0" * 64, "reason_code": ""}
    session = AgentSession(
        id="sess-1",
        role="backend",
        task_ids=["T-1"],
        context_attachments=[attachment],
    )

    events = _record_spawned(session)

    names = [name for name, _data in events]
    assert names.index(CONTEXT_FILES_ATTACHED_EVENT) == names.index("agent_spawned") + 1
    _name, data = events[names.index(CONTEXT_FILES_ATTACHED_EVENT)]
    assert data["agent_id"] == "sess-1"
    assert data["task_ids"] == ["T-1"]
    assert data["entries"] == [attachment]


def test_resumed_tasks_keep_their_declared_context_files(tmp_path: Path) -> None:
    """Crash resume records the declared context exactly like a fresh spawn.

    ``spawn_for_resume`` goes straight to the adapter; the resumed worker
    reads its context from the preserved worktree (the task-specific
    CLAUDE.md written by the original spawn survives the crash and is not
    rewritten). The attachment set must still be re-resolved against that
    worktree and stamped on the session, so ``_record_spawned_events``
    emits ``context.files_attached`` for resumed sessions too. Harness
    mirrors tests/unit/test_crash_recovery.py.
    """
    from unittest.mock import MagicMock

    from bernstein.core.spawner import AgentSpawner

    from bernstein.adapters.base import CLIAdapter, SpawnResult

    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, proc=None, log_path=None)
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=tmp_path / "templates",
        workdir=tmp_path,
        default_model="mock-model",
    )

    worktree = tmp_path / ".sdd" / "worktrees" / "preserved"
    (worktree / "docs").mkdir(parents=True)
    (worktree / "docs" / "adr.md").write_bytes(b"decision\n")

    task = Task(
        id="T-1",
        title="Resume me",
        description="Continue the work.",
        role="backend",
        metadata={"context_files": ["docs/adr.md", "docs/gone.md"]},
    )

    session = spawner.spawn_for_resume([task], worktree_path=worktree, changed_files=["docs/adr.md"])

    # The session carries the resolved attachments, re-resolved against the
    # preserved worktree - identical to what the fresh-spawn path resolves
    # for the same worktree and declaration.
    expected = resolve_context_attachments(root=worktree, declared=collect_declared_context_files([task]))
    assert session.context_attachments == expected
    assert session.context_attachments[0]["sha256"] == "sha256:" + hashlib.sha256(b"decision\n").hexdigest()
    assert session.context_attachments[1]["reason_code"] == REASON_MISSING

    # The resumed session journals the attachment event next to agent_spawned.
    events = _record_spawned(session)
    names = [name for name, _data in events]
    assert names.index(CONTEXT_FILES_ATTACHED_EVENT) == names.index("agent_spawned") + 1


# ---------------------------------------------------------------------------
# No declaration => byte-identical behaviour
# ---------------------------------------------------------------------------


def test_tasks_without_context_files_spawn_byte_identically(tmp_path: Path) -> None:
    """No payload key, no metadata key, identical worker context, no
    context.files_attached record - but skills.injected still fires
    unconditionally with an explicit empty set (issue #3383 AC3)."""
    # Payload: exactly the pre-#3375 shape, no metadata key at all.
    parsed = parse_backlog_text("ticket.md", _TICKET_WITHOUT_CONTEXT)
    assert parsed is not None
    payload = parsed.to_task_payload()
    assert payload == {
        "title": "Wire the auth retry",
        "description": "# Wire the auth retry",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
    }

    # Plan: no context_files key stamped onto task metadata.
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(_PLAN_WITHOUT_CONTEXT, encoding="utf-8")
    _config, plan_tasks = load_plan(plan_file)
    for task in plan_tasks:
        assert "context_files" not in task.metadata

    # Worker context: the wired path produces the same bytes as a build with
    # no declaration wiring at all.
    task = _task_from_payload(payload)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    wired = _write_worker_context(worktree, [task])
    unwired = generate_claude_md(
        [task],
        session_id="sess-1",
        role="backend",
        workdir=worktree,
        context_files=None,
    )
    assert wired == unwired
    assert "## Context files" not in wired

    # Run record: an undeclared session journals exactly what it did
    # before, except skills.injected - unlike context.files_attached,
    # that event fires unconditionally with an explicit empty set (#3383).
    session = AgentSession(id="sess-1", role="backend", task_ids=["T-1"])
    events = _record_spawned(session)
    assert [name for name, _data in events] == ["agent_spawned", "skills.injected", "task_claimed"]
    skills_event = next(data for name, data in events if name == "skills.injected")
    assert skills_event["entries"] == []
    assert skills_event["task_ids"] == ["T-1"]