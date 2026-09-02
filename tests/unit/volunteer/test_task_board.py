"""Tests for the hub-native task board.

The board holds tasks that originate at the hub itself, with no git-forge issue
behind them.  Its identity scheme is the property under test: a hub-native task
id is a content digest under a reserved prefix, so a donor holding a lease can
tell which origin the id it holds belongs to, and a forge-sourced id can never
be mistaken for a board one.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.volunteer.task_board import (
    HUB_TASK_ID_PREFIX,
    HubTask,
    TaskBoard,
    TaskPublishError,
    hub_task_id,
    is_hub_native,
)

if TYPE_CHECKING:
    from pathlib import Path


T0 = 1_700_000_000.0

REPO_URL = "https://example.invalid/proj.git"


class _FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _board(tmp_path: Path, clock: _FakeClock | None = None) -> TaskBoard:
    return TaskBoard(tmp_path / "tasks.jsonl", clock=clock or _FakeClock())


def _publish(
    board: TaskBoard,
    *,
    repo_url: str = REPO_URL,
    title: str = "Fix the thing",
    body: str = "The thing is broken.",
    task_size: str = "s",
    ref: str | None = None,
) -> HubTask:
    return asyncio.run(
        board.publish(
            repo_url=repo_url,
            title=title,
            body=body,
            task_size=task_size,
            ref=ref,
        )
    )


def test_hub_task_id_is_the_digest_of_its_content(tmp_path: Path) -> None:
    """A published task's id is the sha256 of its canonical content."""
    board = _board(tmp_path)
    task = _publish(board)
    expected = hub_task_id(
        repo_url=REPO_URL,
        title="Fix the thing",
        body="The thing is broken.",
        task_size="s",
        ref=None,
    )
    assert task.task_id == expected
    assert task.task_id.startswith(HUB_TASK_ID_PREFIX)
    assert len(task.task_id) == len(HUB_TASK_ID_PREFIX) + 64


def test_two_different_tasks_do_not_share_an_id(tmp_path: Path) -> None:
    """Content that differs in any field lands on a different id."""
    board = _board(tmp_path)
    first = _publish(board, title="Fix the thing")
    second = _publish(board, title="Fix the other thing")
    assert first.task_id != second.task_id
    assert len(board.list_open()) == 2


def test_republishing_identical_content_does_not_create_a_second_task(
    tmp_path: Path,
) -> None:
    """Publishing the same content twice is idempotent, not a duplicate."""
    clock = _FakeClock()
    board = _board(tmp_path, clock)
    first = _publish(board)
    clock.advance(120)
    second = _publish(board)
    assert first.task_id == second.task_id
    assert second.published_at == T0
    assert len(board.list_open()) == 1


def test_published_task_survives_a_restart_of_the_board(tmp_path: Path) -> None:
    """A board rebuilt over the same log still carries the task."""
    board = _board(tmp_path)
    task = _publish(board)
    reopened = TaskBoard(tmp_path / "tasks.jsonl", clock=_FakeClock())
    recovered = reopened.get(task.task_id)
    assert recovered is not None
    assert recovered.title == "Fix the thing"
    assert recovered.repo_url == REPO_URL


def test_forge_sourced_task_id_is_never_read_as_hub_native() -> None:
    """Ids that did not come off the board are outside the reserved prefix."""
    assert not is_hub_native("owner/repo#42")
    assert not is_hub_native("1234")
    assert not is_hub_native("")
    assert is_hub_native(HUB_TASK_ID_PREFIX + "0" * 64)


def test_publish_refuses_a_repo_url_git_must_not_be_handed(tmp_path: Path) -> None:
    """A transport-helper URL is refused at publish, not at clone time."""
    board = _board(tmp_path)
    with pytest.raises(TaskPublishError):
        _publish(board, repo_url="ext::sh -c whoami")
    assert board.list_open() == ()


def test_publish_refuses_an_empty_title(tmp_path: Path) -> None:
    """A task with no title cannot be published."""
    board = _board(tmp_path)
    with pytest.raises(TaskPublishError):
        _publish(board, title="   ")
    assert board.list_open() == ()


def test_board_lists_tasks_in_publish_order(tmp_path: Path) -> None:
    """Listing preserves the order tasks were published in."""
    clock = _FakeClock()
    board = _board(tmp_path, clock)
    titles = ["first", "second", "third"]
    for title in titles:
        clock.advance(10)
        _publish(board, title=title)
    assert [task.title for task in board.list_open()] == titles
    reopened = TaskBoard(tmp_path / "tasks.jsonl", clock=_FakeClock())
    assert [task.title for task in reopened.list_open()] == titles


def test_a_corrupt_record_costs_one_task_not_the_whole_board(tmp_path: Path) -> None:
    """A torn line is skipped on replay rather than being fatal."""
    board = _board(tmp_path)
    kept = _publish(board, title="kept")
    path = tmp_path / "tasks.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    reopened = TaskBoard(path, clock=_FakeClock())
    assert [task.task_id for task in reopened.list_open()] == [kept.task_id]


def test_board_record_is_written_with_stable_canonical_bytes(tmp_path: Path) -> None:
    """Records are serialised sorted and compact, as the lease log is."""
    board = _board(tmp_path)
    _publish(board)
    line = (tmp_path / "tasks.jsonl").read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    assert record["kind"] == "task"
    assert line == json.dumps(record, sort_keys=True, separators=(",", ":"))
