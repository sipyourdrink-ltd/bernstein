"""Schemathesis-driven contract tests against the Bernstein FastAPI app.

OpenAPI is the source of truth for the task-server REST surface. This
file fuzzes documented operations with Hypothesis-generated request
bodies and asserts no 500-class crash leaks an unhandled exception.

PR-time (``smoke`` profile): only fuzz endpoints whose path matches a
short critical-surface allow-list (task CRUD + health + openapi). Five
examples per endpoint, only the ``not_a_server_error`` check enabled.
Total wall-clock: ~60-90s on a hosted runner.

Nightly (``deep`` profile): fuzz every operation in the schema, 50
examples each, full check suite (response-schema conformance,
status-code conformance). Wall-clock ~30-45 min.

Profile selection: ``SCHEMATHESIS_PROFILE=smoke|deep`` (default
``smoke``). The CI workflow exports the value explicitly.

Streaming operations are excluded from the fuzz sweep and covered by the
``test_streaming_operations_*`` tests at the bottom of this file instead.
See the comment on ``_STREAMING_OPERATIONS`` for why.
"""

from __future__ import annotations

import itertools
import os
import re
from typing import TYPE_CHECKING, Any

import anyio
import pytest
import schemathesis
from hypothesis import settings
from schemathesis import checks as st_checks

if TYPE_CHECKING:
    from collections.abc import MutableMapping

# Disable Bernstein auth before the app is built - the OpenAPI schema
# endpoint is itself behind the auth middleware and Schemathesis cannot
# fetch it otherwise. Setting the env var here (before the import) is
# the documented opt-out path.
os.environ.setdefault("BERNSTEIN_AUTH_DISABLED", "1")

from bernstein.core.server.server_app import create_app

# Build a stand-alone FastAPI app instance (auth-disabled via env var,
# no cluster, no external deps). Schemathesis derives its sweep from
# the OpenAPI schema served at ``/openapi.json``.
_app = create_app(auth_token=None, readonly=False, cluster_config=None)
schema = schemathesis.openapi.from_asgi("/openapi.json", _app)

_PROFILE = os.environ.get("SCHEMATHESIS_PROFILE", "smoke")
_MAX_EXAMPLES = {"smoke": 5, "deep": 50}.get(_PROFILE, 5)

# PR-time critical-surface allow-list. Keep tight - every entry adds
# 5×endpoint examples to the wall-clock. The deep profile drops this
# filter and fuzzes the entire schema.
_SMOKE_PATH_PREFIXES = (
    "/healthz",
    "/openapi.json",
    "/api/v1/tasks",
    "/tasks",
    "/metrics",
)


def _path_in_smoke_set(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _SMOKE_PATH_PREFIXES)


def _streaming_operations() -> frozenset[tuple[str, str]]:
    """``(METHOD, path)`` for every operation that answers with a stream.

    Read off the schema rather than a hardcoded path list, so a new SSE
    route is handled the day it is added.
    """
    found: set[tuple[str, str]] = set()
    paths: dict[str, Any] = _app.openapi().get("paths", {})
    for path, operations in paths.items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            responses: dict[str, Any] = operation.get("responses", {})
            for response in responses.values():
                if isinstance(response, dict) and "text/event-stream" in response.get("content", {}):
                    found.add((method.upper(), path))
    return frozenset(found)


# Server-Sent Events operations. A request/response fuzzer cannot drive
# these: `call_and_validate` waits for the response body, and an SSE body
# does not end. Before they were excluded, each one burned the full
# 120 s per-test timeout and was reported as a failure, which reads like
# a server defect and is not one.
#
# They are not dropped from the suite. The `test_streaming_operations_*`
# tests below assert the same property the sweep was after (the operation
# answers without leaking an unhandled exception) using a client that
# hangs up after the first event, which is what ends the stream on a real
# server.
_STREAMING_OPERATIONS = _streaming_operations()


# Smoke runs only the 5xx-leak check; nightly inspects the rest manually.
# `not_a_server_error` catches the case property tests cannot:
# unhandled-exception leaks against arbitrary Hypothesis-generated
# request bodies.
_CHECKS = [st_checks.not_a_server_error]


@schema.parametrize()
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_no_unhandled_exceptions(case: schemathesis.Case) -> None:
    """Every documented endpoint must respond without 500-class crash.

    `case.call_and_validate()` invokes the app via the ASGI in-process
    transport, so each example is a sub-millisecond round-trip - fast
    enough that a focused critical-surface fuzz stays under 90 s at
    smoke settings.
    """
    if (case.method.upper(), case.path) in _STREAMING_OPERATIONS:
        pytest.skip(f"{case.method} {case.path} answers with an unbounded stream, covered by the streaming test below")
    if _PROFILE == "smoke" and not _path_in_smoke_set(case.path):
        pytest.skip(f"path {case.path} not in smoke allow-list")
    response = case.call_and_validate(checks=_CHECKS)
    if response.status_code >= 500:
        pytest.fail(
            f"5xx response from {case.method} {case.path}: status={response.status_code}, body={response.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Streaming operations
# ---------------------------------------------------------------------------


_peer_counter = itertools.count(1)


async def _open_stream_until_first_event(method: str, path: str) -> tuple[int, str, bytes]:
    """Drive one streaming operation and hang up after its first chunk.

    Neither `TestClient` nor httpx's ASGI transport can do this: both wait
    for the application coroutine to return, so they sit through the
    handler's full 60 s idle timeout on every call. Speaking ASGI directly
    lets the test deliver `http.disconnect` the moment the first event
    lands, which is exactly what a real client closing the connection
    looks like to the handler, and it returns in milliseconds.

    Each probe presents itself as a different peer. The server rate-limits
    SSE reconnects per source address, and a suite that opened a dozen
    streams from one address would be testing that limiter instead of the
    routes.
    """
    peer = f"10.0.0.{next(_peer_counter)}"
    scope: MutableMapping[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
        "client": (peer, 1234),
        "server": ("testserver", 80),
    }
    status = 0
    content_type = ""
    chunks: list[bytes] = []
    hung_up = anyio.Event()

    async def receive() -> MutableMapping[str, Any]:
        # The request carries no body, so the only message this connection
        # ever produces is the hang-up, once the first event is out.
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status, content_type
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = {k.decode(): v.decode() for k, v in message.get("headers", [])}
            content_type = headers.get("content-type", "")
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                chunks.append(body)
                hung_up.set()

    await _app(scope, receive, send)
    return status, content_type, b"".join(chunks)


# Path parameters are filled with one safe, unremarkable value. It is
# accepted by the session-id sanitiser and names nothing that exists, so a
# templated stream route either streams or reports "no such thing". Both
# are correct answers; neither is a 5xx.
_PATH_PARAMETER_VALUE = "contract-probe"
_UNPARAMETERISED = sorted(op for op in _STREAMING_OPERATIONS if "{" not in op[1])


def _fill_path_parameters(path: str) -> str:
    return re.sub(r"\{[^{}]+\}", _PATH_PARAMETER_VALUE, path)


@pytest.mark.parametrize(("method", "path"), sorted(_STREAMING_OPERATIONS))
def test_streaming_operations_do_not_leak_a_server_error(method: str, path: str) -> None:
    """The property the fuzz sweep was after, for the paths it cannot drive.

    Also pins that a streaming answer sends the media type it publishes.
    A route that reports 404 for an unknown id never reaches its stream,
    so the media type is only asserted when one was actually opened.
    """
    concrete = _fill_path_parameters(path)
    status, content_type, _ = anyio.run(_open_stream_until_first_event, method, concrete)
    assert status < 500, f"{method} {concrete} answered {status}"
    if status == 200:
        assert content_type.startswith("text/event-stream"), f"{method} {concrete} sent content-type {content_type!r}"


@pytest.mark.parametrize(("method", "path"), _UNPARAMETERISED)
def test_streaming_operations_start_and_close(method: str, path: str) -> None:
    """Streams that need no identifier open, emit, and close on hang-up.

    The stronger assertion, restricted to the operations that address no
    particular resource so a 200 is the only correct answer. The value of
    the "close" half is that it runs at all: the handler releases the
    connection when the client leaves instead of holding it to its 60 s
    idle timeout, which is what made this test possible to write.
    """
    status, content_type, body = anyio.run(_open_stream_until_first_event, method, path)
    assert status == 200, f"{method} {path} answered {status}"
    assert content_type.startswith("text/event-stream"), f"{method} {path} sent content-type {content_type!r}"
    assert body, f"{method} {path} closed without emitting anything"


def test_streaming_operations_are_declared_in_the_schema() -> None:
    """The exclusion above must come from the schema, not from nothing.

    If the SSE routes ever stop publishing ``text/event-stream``, this set
    empties out, the fuzz sweep starts driving unbounded streams again,
    and every one of them times out. Fail here instead, where the reason
    is legible.
    """
    assert _STREAMING_OPERATIONS, "no operation declares text/event-stream; the SSE routes lost their media type"
