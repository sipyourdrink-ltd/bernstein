"""Liveness classification behind ``bernstein migrate``'s chunk reuse (#4624).

``spawn_swarm`` reuses a chunk's existing task while the store reports it
active, and respawns only once it is gone or terminal. The production store is
:class:`~bernstein.cli.commands.migrate_cmd._ServerTaskStore`, which answers
that question by reading ``GET /tasks/{id}`` and comparing the reported status
against ``_TERMINAL_STATUS_VALUES``.

The classification is the whole guarantee. Get it wrong in one direction and a
mid-swarm re-run hands a second agent files another agent is still editing
(#4624); wrong in the other and a chunk whose task can never move again is
reported active forever, so ``spawn_swarm`` returns a dead id on every
subsequent run - the failure #4541 was written to prevent.

These tests drive the real store through the real ``spawn_swarm`` with the HTTP
layer stubbed, so what is pinned is the status classification rather than a
test double's memory of which ids it handed out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.cli.commands import migrate_cmd
from bernstein.cli.commands.migrate_cmd import _TERMINAL_STATUS_VALUES, _ServerTaskStore
from bernstein.core.tasks.lifecycle import TASK_TRANSITIONS
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.swarm_migration import MigrationPlan, spawn_swarm


class _FakeServer:
    """The task server's ``POST /tasks`` and ``GET /tasks/{id}``, in memory.

    ``statuses`` maps a task id to the status the server reports for it. An id
    absent from the mapping is a ``404`` - the task is gone.
    """

    def __init__(self, spawn_status: str = TaskStatus.IN_PROGRESS.value) -> None:
        self.statuses: dict[str, str] = {}
        self.created: list[dict[str, Any]] = []
        self._spawn_status = spawn_status
        self._counter = 0

    def post(self, path: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        assert path == "/tasks"
        self._counter += 1
        task_id = f"task-{self._counter:03d}"
        self.created.append(payload)
        self.statuses[task_id] = self._spawn_status
        return {"id": task_id}

    def get(self, path: str, **_kwargs: Any) -> dict[str, Any] | None:
        task_id = path.removeprefix("/tasks/")
        status = self.statuses.get(task_id)
        return None if status is None else {"id": task_id, "status": status}


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    server = _FakeServer()
    monkeypatch.setattr(migrate_cmd, "server_post", server.post)
    monkeypatch.setattr(migrate_cmd, "server_get", server.get)
    return server


def _make_repo(tmp_path: Path, files: list[str]) -> Path:
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# placeholder\n", encoding="utf-8")
    return tmp_path


def test_suspended_chunk_respawns(tmp_path: Path, fake_server: _FakeServer) -> None:
    """A chunk parked in SUSPENDED must respawn, not be reused forever (#4541).

    SUSPENDED has no outgoing transition, so a task in it will never move again
    - but it is in neither dependency-classification set, so a terminal set
    built from those two alone reports it active on every re-run and the chunk
    is stranded behind a dead id. This is the same class of bug as #4624 seen
    from the other side, and it fails against the two-set union.
    """
    repo = _make_repo(tmp_path, ["src/a.py", "src/b.py"])
    plan = MigrationPlan(id="suspended", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _ServerTaskStore()

    first_ids = spawn_swarm(plan, store, repo)
    assert len(first_ids) == 2

    # Chunk 0 is suspended - it will never move again. Chunk 1 keeps running.
    fake_server.statuses[first_ids[0]] = TaskStatus.SUSPENDED.value
    second_ids = spawn_swarm(plan, store, repo)

    assert second_ids[0] != first_ids[0], "a suspended chunk can never move again and must respawn"
    assert second_ids[1] == first_ids[1], "a running chunk is still reused"
    assert len(fake_server.created) == 3, "exactly one respawn"


def test_in_flight_chunk_is_still_reused(tmp_path: Path, fake_server: _FakeServer) -> None:
    """The widened terminal set must not start respawning live chunks (#4624).

    Guards the fix above from the cheap way to pass it: classifying everything
    as terminal would satisfy the suspended case and reintroduce the duplicate
    owner this whole path exists to prevent.
    """
    repo = _make_repo(tmp_path, ["src/a.py", "src/b.py"])
    plan = MigrationPlan(id="inflight", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _ServerTaskStore()

    first_ids = spawn_swarm(plan, store, repo)
    for status in (TaskStatus.CLAIMED, TaskStatus.BLOCKED, TaskStatus.PENDING_APPROVAL):
        fake_server.statuses[first_ids[0]] = status.value
        assert spawn_swarm(plan, store, repo) == first_ids, f"{status.value} may still be editing its files"
    assert len(fake_server.created) == 2, "no chunk was respawned"


def test_missing_task_respawns(tmp_path: Path, fake_server: _FakeServer) -> None:
    """A ``404`` is a task that is gone, so the chunk respawns (#4624 docstring)."""
    repo = _make_repo(tmp_path, ["src/a.py"])
    plan = MigrationPlan(id="gone", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _ServerTaskStore()

    first_ids = spawn_swarm(plan, store, repo)
    del fake_server.statuses[first_ids[0]]

    assert spawn_swarm(plan, store, repo) != first_ids
    assert len(fake_server.created) == 2


def test_unreachable_server_is_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable server yields ``None`` too and is treated as not-active."""
    monkeypatch.setattr(migrate_cmd, "server_get", lambda _path, **_kw: None)
    assert _ServerTaskStore().is_task_active("task-001") is False


def test_terminal_status_values_covers_every_unresumable_status() -> None:
    """Every status is either classified terminal or genuinely able to move on.

    A status that ``_TERMINAL_STATUS_VALUES`` treats as active while the FSM
    gives it no outgoing transition strands its chunk permanently. Pinning the
    property rather than the one instance means a status added to only one of
    the three lifecycle sets - or to none of them - fails here instead of in a
    swarm.
    """
    resumable = {frm for (frm, _to) in TASK_TRANSITIONS}
    stranded = [s.value for s in TaskStatus if s.value not in _TERMINAL_STATUS_VALUES and s not in resumable]
    assert stranded == [], f"unresumable statuses reported active forever: {sorted(stranded)}"
