"""Worker re-queues a claimed task when the agent spawn fails (#3018).

Before this fix, ``_claim_available_tasks`` claimed a task, called
``_spawn_agent``, and -- when the spawn failed (returned ``None``) -- simply
dropped the task on the floor: it was never added to ``_active_tasks`` and was
never released, so it stayed ``claimed`` on the server forever with no live
agent. The worker now releases the claim back to the open pool so another node
can take it.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from bernstein.cli.commands.worker_cmd import WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path


def _make_loop(tmp_path: Path) -> WorkerLoop:
    return WorkerLoop(
        server_url="http://central:8052",
        name="test-node",
        auth_token="secret-token",
        adapter="claude",
        roles=["backend"],
        workdir=tmp_path,
    )


class TestReleaseOnSpawnFailure:
    def test_spawn_failure_releases_claim_back_to_pool(self, tmp_path: Path) -> None:
        """A failed spawn posts to /tasks/{id}/release so the task re-queues."""
        loop = _make_loop(tmp_path)
        client = mock.MagicMock()
        # One claimable task for the single "backend" role, then nothing.
        client.get.return_value = mock.MagicMock(
            status_code=200,
            json=lambda: {"id": "task-77", "title": "do it", "role": "backend"},
        )

        with mock.patch.object(loop, "_spawn_agent", return_value=None):
            loop._claim_available_tasks(client, node_id="node-1")

        # The claim was released (re-queued), and the slot was NOT consumed.
        release_calls = [
            call for call in client.post.call_args_list if call.args and call.args[0].endswith("/tasks/task-77/release")
        ]
        assert release_calls, f"expected a release POST, got: {client.post.call_args_list}"
        assert "task-77" not in loop._active_tasks

    def test_spawn_success_does_not_release(self, tmp_path: Path) -> None:
        """A successful spawn consumes the slot and never releases the task."""
        loop = _make_loop(tmp_path)
        client = mock.MagicMock()
        client.get.return_value = mock.MagicMock(
            status_code=200,
            json=lambda: {"id": "task-88", "title": "do it", "role": "backend"},
        )

        with mock.patch.object(loop, "_spawn_agent", return_value=4242):
            loop._claim_available_tasks(client, node_id="node-1")

        release_calls = [
            call
            for call in client.post.call_args_list
            if call.args and str(call.args[0]).endswith("/tasks/task-88/release")
        ]
        assert not release_calls
        assert loop._active_tasks.get("task-88") == 4242

    def test_release_task_posts_reason_and_headers(self, tmp_path: Path) -> None:
        """_release_task targets the release route with a reason and auth header."""
        loop = _make_loop(tmp_path)
        client = mock.MagicMock()

        loop._release_task(client, "task-99", "agent spawn failed")

        (url,), kwargs = client.post.call_args
        assert url == "http://central:8052/tasks/task-99/release"
        assert kwargs["json"] == {"reason": "agent spawn failed"}
        assert kwargs["headers"]["Authorization"] == "Bearer secret-token"
