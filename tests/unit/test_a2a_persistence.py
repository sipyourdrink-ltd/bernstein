"""``A2AHandler`` state survives a restart when persistence is enabled (#2609).

The module docstring in ``a2a.py`` flagged that the in-memory handler loses
every A2A task on server restart, which makes an inbound task and its receipt
unrecoverable. Persistence is opt-in: the default backend stays in-memory so
existing callers and tests are untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.protocols.a2a.a2a import A2AHandler, A2ATaskStatus

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Default backend stays in-memory
# ---------------------------------------------------------------------------


def test_default_handler_does_not_touch_disk(tmp_path: Path) -> None:
    """No ``state_path`` means no filesystem writes at all."""
    handler = A2AHandler(server_url="http://localhost:8052")
    handler.create_task(sender="peer", message="do the thing")

    assert list(tmp_path.iterdir()) == []
    assert handler.state_path is None


def test_default_handler_loses_state_across_instances() -> None:
    """Documents the pre-existing (and still default) behaviour."""
    first = A2AHandler(server_url="http://localhost:8052")
    task = first.create_task(sender="peer", message="do the thing")

    second = A2AHandler(server_url="http://localhost:8052")

    assert second.get_task(task.id) is None


# ---------------------------------------------------------------------------
# AC: handler state survives a restart
# ---------------------------------------------------------------------------


def test_task_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    task = first.create_task(sender="peer.example", message="do the thing")

    # "Restart": a brand new handler over the same state file.
    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    recovered = second.get_task(task.id)

    assert recovered is not None
    assert recovered.id == task.id
    assert recovered.sender == "peer.example"
    assert recovered.message == "do the thing"
    assert recovered.status is A2ATaskStatus.SUBMITTED


def test_bernstein_task_link_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    task = first.create_task(sender="peer", message="m")
    first.link_bernstein_task(task.id, "bernstein-task-7")

    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)

    assert second.get_by_bernstein_id("bernstein-task-7") is not None
    assert second.get_by_bernstein_id("bernstein-task-7").id == task.id


def test_status_sync_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    task = first.create_task(sender="peer", message="m")
    first.sync_status(task.id, "done")

    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)

    assert second.get_task(task.id).status is A2ATaskStatus.COMPLETED


def test_artifacts_survive_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    task = first.create_task(sender="peer", message="m")
    first.add_artifact(task.id, "result.txt", "the answer", content_type="text/plain")

    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    recovered = second.get_task(task.id)

    assert len(recovered.artifacts) == 1
    assert recovered.artifacts[0].name == "result.txt"
    assert recovered.artifacts[0].data == "the answer"
    assert recovered.artifacts[0].content_type == "text/plain"


def test_messages_survive_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    first.receive_message(sender="a", recipient="b", content="hello", task_id="t-1")

    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    messages = second.list_messages(task_id="t-1")

    assert len(messages) == 1
    assert messages[0].content == "hello"
    assert messages[0].direction == "inbound"


def test_receipts_survive_restart(tmp_path: Path) -> None:
    """An inbound task's receipt is recoverable after a restart."""
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    task = first.create_task(sender="peer", message="m")
    first.attach_receipt(task.id, {"entry_hash": "sha256:aa", "content_hash": "sha256:bb"})

    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)

    assert second.get_receipt(task.id) == {"entry_hash": "sha256:aa", "content_hash": "sha256:bb"}


def test_list_tasks_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "a2a-state.json"
    first = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    first.create_task(sender="peer-a", message="one")
    first.create_task(sender="peer-b", message="two")

    second = A2AHandler(server_url="http://localhost:8052", state_path=state_path)

    assert len(second.list_tasks()) == 2
    assert len(second.list_tasks(sender="peer-a")) == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_missing_state_file_starts_empty(tmp_path: Path) -> None:
    handler = A2AHandler(server_url="http://localhost:8052", state_path=tmp_path / "absent.json")
    assert handler.list_tasks() == []


def test_corrupt_state_file_starts_empty_rather_than_crashing(tmp_path: Path) -> None:
    """A truncated state file must not take the whole server down on boot."""
    state_path = tmp_path / "a2a-state.json"
    state_path.write_text("{not json", encoding="utf-8")

    handler = A2AHandler(server_url="http://localhost:8052", state_path=state_path)

    assert handler.list_tasks() == []


def test_state_directory_is_created_on_demand(tmp_path: Path) -> None:
    state_path = tmp_path / "nested" / "dir" / "a2a-state.json"
    handler = A2AHandler(server_url="http://localhost:8052", state_path=state_path)
    handler.create_task(sender="peer", message="m")

    assert state_path.exists()


def test_link_to_unknown_task_still_raises(tmp_path: Path) -> None:
    """Persistence must not soften the existing error contract."""
    handler = A2AHandler(server_url="http://localhost:8052", state_path=tmp_path / "s.json")
    with pytest.raises(KeyError):
        handler.link_bernstein_task("nope", "bernstein-1")
