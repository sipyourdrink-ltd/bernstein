"""No MCP tool reaches a terminal task route without the gate (#3081).

The approval gate is only worth its refusal if ``bernstein_approve`` is the
only way to reach ``POST /tasks/{id}/complete``. Gating one handler while a
second tool posts the same URL unconditionally moves the force-complete
rather than removing it, and the tool description a model reads is not what
constrains it.

So the property asserted here is about the whole advertised surface, not
about the two handlers that carry the gate: every tool both transports
advertise is driven once per ``TaskStatus``, and the requests it issues are
recorded. A request to a route that finishes or re-queues a task
(``/complete``, ``/force-claim``) is allowed only from the two gated verbs,
and only from the states the state machine defines them for.

Both tables are guarded for completeness against the advertised tool list, so
a newly added tool cannot join the surface without being swept.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.tasks.lifecycle import (
    APPROVABLE_TASK_STATUSES,
    WORKER_COMPLETABLE_TASK_STATUSES,
)
from bernstein.core.tasks.models import TaskStatus

if TYPE_CHECKING:
    from pathlib import Path

_TASK_ID = "task-sweep-1"
_SERVER_URL = "http://localhost:8052"

#: Route suffixes that finish a task or push it back into the run queue.
#: Reaching one of these decides work, so it may only happen behind a gate.
_TERMINAL_ROUTES: tuple[str, ...] = ("/complete", "/force-claim", "/fail", "/cancel")


#: (tool, status) pairs allowed to reach a terminal route, derived from the
#: state machine so the allowance cannot drift from what the gates enforce.
def _cancellable_statuses() -> frozenset[str]:
    from bernstein.mcp.server import _CANCELLABLE_STATUSES

    return _CANCELLABLE_STATUSES


_ALLOWED: set[tuple[str, str]] = (
    {("bernstein_approve", s.value) for s in APPROVABLE_TASK_STATUSES}
    | {("bernstein_complete", s.value) for s in WORKER_COMPLETABLE_TASK_STATUSES}
    | {("bernstein_cancel", s) for s in _cancellable_statuses()}
)

#: The same allowance written out, so the sweep is not measured against the
#: constant it is supposed to police. Deriving ``_ALLOWED`` keeps a newly
#: added ``TaskStatus`` covered automatically, but on its own it would also
#: absorb a widening of either set and report no offenders.
_ALLOWED_LITERAL: set[tuple[str, str]] = {
    ("bernstein_approve", "pending_approval"),
    ("bernstein_complete", "open"),
    ("bernstein_complete", "claimed"),
    ("bernstein_complete", "in_progress"),
    # bernstein_cancel (#3078) reaches /cancel only from the states the
    # route itself accepts, gated by the same read-before-act pattern.
    ("bernstein_cancel", "open"),
    ("bernstein_cancel", "claimed"),
    ("bernstein_cancel", "in_progress"),
    ("bernstein_cancel", "blocked"),
    ("bernstein_cancel", "waiting_for_subtasks"),
    ("bernstein_cancel", "planned"),
}


def test_the_swept_allowance_is_the_policy_and_not_whatever_the_code_says() -> None:
    """Widening either gate has to fail here, not silently widen the sweep.

    ``_ALLOWED`` is projected from the state machine, so a change that adds a
    status to the approvable or completable set would move the sweep's own
    yardstick with it. This pins the policy independently: a task may be
    finished from a state a worker holds it in, and signed off from the state
    that holds a finished result. Nothing else, from no tool.
    """
    assert _ALLOWED == _ALLOWED_LITERAL, (
        "an MCP verb may now reach a terminal task route from a state this "
        "policy does not allow; if the widening is intended, change the "
        "policy here deliberately"
    )


def _stdio_args(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Minimal valid arguments for every tool the stdio server advertises."""
    return {
        # The consolidated surface (#3087).
        "bernstein_status": {},
        "bernstein_run": {"goal": "ship the parser"},
        "bernstein_run_status": {"run_id": "run-1", "workdir": str(tmp_path)},
        "bernstein_claim": {"claimer_id": "worker-1"},
        "bernstein_post_message": {"task_id": _TASK_ID, "body": "still working", "sender": "worker-1"},
        "bernstein_post_artifact": {
            "task_id": _TASK_ID,
            "key": "report",
            "artifact_type": "report",
            "poster": "worker-1",
            "body": "findings",
        },
        "bernstein_cancel": {"task_id": _TASK_ID},
        "bernstein_shutdown_orchestrator": {"workdir": str(tmp_path)},
        "bernstein_approve": {"task_id": _TASK_ID, "note": "unstick it"},
        "bernstein_complete": {"task_id": _TASK_ID, "result_summary": "looked done to me"},
        "bernstein_task_capsule": {"task_id": _TASK_ID, "workdir": str(tmp_path)},
        "load_skill": {"name": "backend"},
        # Deprecated aliases, callable for one release (#3087): swept with
        # the same terminal-route policy as their replacements.
        "bernstein_health": {},
        "bernstein_cost": {},
        "bernstein_tasks": {"status": "open"},
        "bernstein_update": {"task_id": _TASK_ID, "body": "still working", "sender": "worker-1"},
        "bernstein_stop": {"workdir": str(tmp_path)},
        "bernstein_create_subtask": {"parent_task_id": _TASK_ID, "goal": "split the work"},
        "bernstein_task_handle": {"run_id": "run-1", "workdir": str(tmp_path)},
        "bernstein_context": {"task_id": _TASK_ID, "workdir": str(tmp_path)},
    }


def _remote_args() -> dict[str, dict[str, Any]]:
    """Minimal valid arguments for every tool the HTTP transport advertises."""
    return {
        "bernstein_status": {},
        "bernstein_run": {"goal": "ship the parser"},
        "bernstein_cancel": {"task_id": _TASK_ID},
        "bernstein_shutdown_orchestrator": {"workdir": ""},
        "bernstein_approve": {"task_id": _TASK_ID, "note": "unstick it"},
        "bernstein_complete": {"task_id": _TASK_ID, "result_summary": "looked done to me"},
    }


def _mock_http_client(status: str, posts: list[str]) -> MagicMock:
    """An httpx client whose GETs report *status* and whose POSTs are recorded."""
    read = MagicMock()
    read.status_code = 200
    read.raise_for_status = MagicMock()
    read.json = MagicMock(return_value={"id": _TASK_ID, "status": status, "role": "backend"})
    read.text = json.dumps({"id": _TASK_ID, "status": status})

    written = MagicMock()
    written.status_code = 200
    written.raise_for_status = MagicMock()
    written.json = MagicMock(return_value={"id": _TASK_ID, "status": "done", "result_summary": "x", "seq": 1})
    written.text = "{}"

    async def _post(url: str, **_kwargs: object) -> MagicMock:
        posts.append(url)
        return written

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=read)
    client.post = AsyncMock(side_effect=_post)
    return client


def _forbidden(posts: list[str], tool: str, status: str) -> list[str]:
    """Requests to a terminal route this (tool, status) pair may not make."""
    if (tool, status) in _ALLOWED:
        return []
    return [url for url in posts if any(url.endswith(route) for route in _TERMINAL_ROUTES)]


# ---------------------------------------------------------------------------
# stdio server
# ---------------------------------------------------------------------------


def test_every_stdio_tool_is_covered_by_the_sweep(tmp_path: Path) -> None:
    """A tool added without a row here would never be swept, so fail loudly."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url=_SERVER_URL)
    advertised = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert advertised == set(_stdio_args(tmp_path)), (
        "the advertised stdio tool list and the sweep table disagree; add the new "
        "tool to _stdio_args so it is checked against the terminal routes"
    )


@pytest.mark.parametrize("status", sorted(s.value for s in TaskStatus))
@pytest.mark.asyncio
async def test_no_stdio_tool_finishes_a_task_outside_the_gate(status: str, tmp_path: Path) -> None:
    """Drive every advertised tool against a task in *status* and watch the writes."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url=_SERVER_URL)
    offenders: dict[str, list[str]] = {}

    for tool, args in _stdio_args(tmp_path).items():
        posts: list[str] = []
        client = _mock_http_client(status, posts)
        with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
            await mcp.call_tool(tool, dict(args))
        bad = _forbidden(posts, tool, status)
        if bad:
            offenders[tool] = bad

    assert offenders == {}, (
        f"with the task in {status!r}, these tools reached a terminal task route "
        f"without a gate that allows it: {offenders}"
    )


@pytest.mark.parametrize("status", sorted(s.value for s in TaskStatus))
@pytest.mark.asyncio
async def test_the_stdio_gates_do_act_in_the_states_they_allow(status: str, tmp_path: Path) -> None:
    """The mirror image: a gate that refuses everything would pass the sweep.

    Without this the containment above is satisfied by a tool that never
    works, so the allowed pairs are asserted to actually issue the write.
    """
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url=_SERVER_URL)

    for tool, args in _stdio_args(tmp_path).items():
        if (tool, status) not in _ALLOWED:
            continue
        posts: list[str] = []
        client = _mock_http_client(status, posts)
        with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
            await mcp.call_tool(tool, dict(args))
        expected_route = "/cancel" if tool == "bernstein_cancel" else "/complete"
        assert any(url.endswith(expected_route) for url in posts), (
            f"{tool} is defined for {status!r} but issued no {expected_route} write: {posts}"
        )


# ---------------------------------------------------------------------------
# streamable HTTP transport
# ---------------------------------------------------------------------------


def test_every_remote_tool_is_covered_by_the_sweep() -> None:
    """Same completeness guard for the second transport."""
    from bernstein.mcp.remote_transport import _TOOL_DEFS

    advertised = {definition["name"] for definition in _TOOL_DEFS}
    assert advertised == set(_remote_args()), (
        "the advertised HTTP tool list and the sweep table disagree; add the new "
        "tool to _remote_args so it is checked against the terminal routes"
    )


@pytest.mark.parametrize("status", sorted(s.value for s in TaskStatus))
@pytest.mark.asyncio
async def test_no_remote_tool_finishes_a_task_outside_the_gate(status: str) -> None:
    """The HTTP transport must not be the cheaper way to the same routes."""
    from bernstein.mcp.remote_transport import RemoteMCPConfig, StreamableHTTPTransport

    transport = StreamableHTTPTransport(
        config=RemoteMCPConfig(path="/mcp", auth_type="none"),
        server_url=_SERVER_URL,
    )
    offenders: dict[str, list[str]] = {}

    for tool, args in _remote_args().items():
        posts: list[str] = []

        async def _post(path: str, _payload: object = None, *, _sink: list[str] = posts) -> str:
            _sink.append(path)
            return "{}"

        proxy_get = AsyncMock(return_value=json.dumps({"id": _TASK_ID, "status": status}))
        with (
            patch.object(StreamableHTTPTransport, "_proxy_get", proxy_get),
            patch.object(StreamableHTTPTransport, "_proxy_post", AsyncMock(side_effect=_post)),
        ):
            # A tool that raises issued no write, which is what is being measured.
            with contextlib.suppress(Exception):
                await transport._execute_tool(tool, dict(args))
        bad = _forbidden(posts, tool, status)
        if bad:
            offenders[tool] = bad

    assert offenders == {}, (
        f"with the task in {status!r}, these HTTP tools reached a terminal task "
        f"route without a gate that allows it: {offenders}"
    )
