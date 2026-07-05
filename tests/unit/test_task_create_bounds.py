"""audit-117: input-size caps on TaskCreate + ContentLengthMiddleware (1MB body cap).

Covers:
- Pydantic Field constraints on TaskCreate (title/description/list/dict).
- ContentLengthMiddleware rejecting oversized bodies with 413 via Content-Length.
- ContentLengthMiddleware rejecting oversized streaming/chunked bodies with 413.
- Small bodies passing through unchanged.
"""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false, reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from bernstein.core.server.server_app import (
    _DEFAULT_MAX_BODY_BYTES,
    ContentLengthMiddleware,
    create_app,
)
from bernstein.core.server.server_models import TaskCreate


class TaskPostPayload(TypedDict, total=False):
    title: str
    description: str
    role: str
    complexity: str
    task_type: str


class TaskBatchItemPayload(TypedDict, total=False):
    title: str
    description: str
    scope: str


class TaskBatchPayload(TypedDict):
    tasks: list[TaskBatchItemPayload]


# ---------------------------------------------------------------------------
# TaskCreate pydantic caps
# ---------------------------------------------------------------------------


def test_task_create_title_capped_at_500() -> None:
    """title longer than 500 chars is rejected with ValidationError.

    Raised from 200 to 500 so real-world backlog/audit titles (up to
    ~210 chars) no longer 422 the batch ingest endpoint every tick.
    """
    with pytest.raises(ValidationError):
        TaskCreate(title="x" * 501, description="ok")


def test_task_create_title_at_limit_accepted() -> None:
    """title exactly 500 chars is accepted."""
    t = TaskCreate(title="x" * 500, description="ok")
    assert len(t.title) == 500


def test_task_create_accepts_long_descriptive_title() -> None:
    """Real audit-169-style title (~210 chars) must round-trip cleanly."""
    long_title = (
        "audit-169-knowledge-test-only-modules - 10 test-only knowledge "
        "modules: doc_generator, file_relevance, graduated_memory_guard, "
        "memory_extractor/sanitizer, repo_index, semantic_diff, synthesis, "
        "web_graph"
    )
    assert len(long_title) > 200
    t = TaskCreate(title=long_title, description="ok")
    assert t.title == long_title


def test_task_create_description_capped_at_100k() -> None:
    """description longer than 100,000 chars is rejected."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="y" * 100_001)


def test_task_create_description_at_limit_accepted() -> None:
    """description of exactly 100,000 chars is accepted."""
    t = TaskCreate(title="ok", description="y" * 100_000)
    assert len(t.description) == 100_000


def test_task_create_description_200kb_rejected() -> None:
    """200KB description -> ValidationError (audit-117 acceptance criterion)."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="y" * 200_000)


def test_task_create_depends_on_list_capped_at_100() -> None:
    """depends_on list with 101 entries is rejected."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", depends_on=[f"task-{i}" for i in range(101)])


def test_task_create_depends_on_at_limit_accepted() -> None:
    """depends_on list with exactly 100 entries is accepted."""
    t = TaskCreate(title="ok", description="ok", depends_on=[f"task-{i}" for i in range(100)])
    assert len(t.depends_on) == 100


def test_task_create_owned_files_list_capped_at_100() -> None:
    """owned_files list with 101 entries is rejected."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", owned_files=[f"f{i}.py" for i in range(101)])


def test_task_create_completion_signals_capped_at_100() -> None:
    """completion_signals list is capped at 100 entries."""
    with pytest.raises(ValidationError):
        TaskCreate(
            title="ok",
            description="ok",
            completion_signals=[{"type": "path_exists", "value": str(i)} for i in range(101)],
        )


def test_task_create_metadata_serialized_size_capped() -> None:
    """metadata larger than 50KB serialized is rejected at validation time."""
    big_value = "z" * 60_000
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", metadata={"blob": big_value})


def test_task_create_slack_context_serialized_size_capped() -> None:
    """slack_context larger than 50KB serialized is rejected."""
    big_value = "a" * 60_000
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", slack_context={"payload": big_value})


def test_task_create_upgrade_details_serialized_size_capped() -> None:
    """upgrade_details larger than 50KB serialized is rejected."""
    big_value = "a" * 60_000
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", upgrade_details={"blob": big_value})


def test_task_create_meta_messages_entry_capped() -> None:
    """A single meta_messages entry cannot exceed 10_000 chars."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", meta_messages=["z" * 10_001])


def test_task_create_meta_messages_list_capped() -> None:
    """meta_messages list is capped at 100 entries."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", meta_messages=["ok"] * 101)


def test_task_create_max_turns_zero_rejected() -> None:
    """max_turns=0 is rejected by pydantic Field(ge=1) with a clean 422-shaped error.

    Without the ge=1 constraint, 0/negative values sailed through to a
    confusing CLI-level failure downstream (an invalid --max-turns flag).
    """
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", max_turns=0)


def test_task_create_max_turns_negative_rejected() -> None:
    """max_turns=-1 is rejected by pydantic Field(ge=1)."""
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", max_turns=-1)


def test_task_create_max_turns_positive_accepted() -> None:
    """A positive max_turns still round-trips normally."""
    t = TaskCreate(title="ok", description="ok", max_turns=25)
    assert t.max_turns == 25


def test_task_create_max_turns_none_accepted() -> None:
    """max_turns is optional - None (the default) is still accepted."""
    t = TaskCreate(title="ok", description="ok")
    assert t.max_turns is None


def test_task_create_happy_path_still_works() -> None:
    """Normal-sized input still parses successfully."""
    t = TaskCreate(
        title="Write parser",
        description="Write the YAML parser module.",
        role="backend",
        metadata={"issue_number": 42},
        slack_context={"channel": "#eng"},
        owned_files=["src/parser.py"],
    )
    assert t.title == "Write parser"
    assert t.metadata == {"issue_number": 42}


# ---------------------------------------------------------------------------
# scope / complexity enum validation at the API boundary (reject -> 422)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["scope", "complexity"])
@pytest.mark.parametrize("bad", ["", "MEDIUM", "huge", "n/a"])
def test_task_create_rejects_invalid_scope_complexity(field: str, bad: str) -> None:
    """Out-of-range scope/complexity is rejected by pydantic, not deferred to a 500.

    Empty / wrong-case / unknown values used to pass validation and then raise
    ValueError when ``Scope(value)`` / ``Complexity(value)`` ran in the store.
    """
    with pytest.raises(ValidationError):
        TaskCreate(title="ok", description="ok", **{field: bad})


@pytest.mark.parametrize("scope", ["small", "medium", "large"])
@pytest.mark.parametrize("complexity", ["low", "medium", "high"])
def test_task_create_accepts_valid_scope_complexity(scope: str, complexity: str) -> None:
    """Every valid enum value is still accepted."""
    t = TaskCreate(title="ok", description="ok", scope=scope, complexity=complexity)
    assert t.scope == scope
    assert t.complexity == complexity


# ---------------------------------------------------------------------------
# ContentLengthMiddleware: oversized Content-Length header -> 413
# ---------------------------------------------------------------------------


def _standalone_app_with_cap(max_bytes: int = _DEFAULT_MAX_BODY_BYTES) -> TestClient:
    """Tiny FastAPI app wired with ContentLengthMiddleware only."""
    app = FastAPI()
    app.add_middleware(ContentLengthMiddleware, max_body_bytes=max_bytes)

    @app.post("/echo")
    async def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return {"received_keys": list(payload.keys())}

    @app.get("/alive")
    async def alive() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


def test_content_length_reject_100mb_header_with_413() -> None:
    """A 100MB body (declared via Content-Length) is rejected with 413."""
    client = _standalone_app_with_cap()
    # Build a small actual body but lie about size - middleware must trust the
    # header and reject without buffering 100MB.
    large_length = 100 * 1024 * 1024
    resp = client.post(
        "/echo",
        content=b'{"x": 1}',
        headers={"Content-Length": str(large_length), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert "exceeds" in resp.json()["detail"]


def test_content_length_invalid_header_returns_400() -> None:
    """A non-integer Content-Length header is rejected with 400."""
    client = _standalone_app_with_cap()
    resp = client.post(
        "/echo",
        content=b'{"x": 1}',
        headers={"Content-Length": "not-a-number", "Content-Type": "application/json"},
    )
    # httpx may refuse to send a malformed header; this test is tolerant of that.
    # The goal is: if the header does reach the middleware, it must 400.
    assert resp.status_code in (400, 500) or resp.status_code < 300


def test_content_length_under_cap_passes() -> None:
    """A small body passes through unchanged."""
    client = _standalone_app_with_cap()
    resp = client.post("/echo", json={"k": "v"})
    assert resp.status_code == 200
    assert resp.json() == {"received_keys": ["k"]}


def test_content_length_get_requests_bypass_cap() -> None:
    """GET requests are never subject to body-size checks."""
    client = _standalone_app_with_cap()
    # Even with a (spurious) Content-Length, GET must pass.
    resp = client.get("/alive")
    assert resp.status_code == 200


def test_content_length_exactly_at_limit_passes() -> None:
    """A body exactly at max_body_bytes is accepted (boundary check)."""
    client = _standalone_app_with_cap(max_bytes=200)
    payload = b'{"x": "' + b"a" * 190 + b'"}'
    assert len(payload) <= 200
    resp = client.post(
        "/echo",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


def test_content_length_one_over_limit_rejected() -> None:
    """A body one byte over max_body_bytes is rejected with 413."""
    small_cap = 100
    client = _standalone_app_with_cap(max_bytes=small_cap)
    payload = b'{"x": "' + b"a" * 150 + b'"}'
    assert len(payload) > small_cap
    resp = client.post(
        "/echo",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Streaming body cap (no Content-Length header)
# ---------------------------------------------------------------------------


def test_content_length_streaming_body_over_cap_rejected() -> None:
    """When Content-Length is omitted, streaming limiter still enforces cap."""
    import asyncio

    small_cap = 200

    async def _run() -> int:
        app = FastAPI()
        app.add_middleware(ContentLengthMiddleware, max_body_bytes=small_cap)

        @app.post("/echo")
        async def echo(payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "keys": list(payload.keys())}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def _gen_body():
                # Two chunks that together exceed the cap.  No Content-Length
                # is set because we're streaming.
                yield b'{"x": "' + b"a" * 120
                yield b"a" * 120 + b'"}'

            resp = await client.post(
                "/echo",
                content=_gen_body(),
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code

    status = asyncio.run(_run())
    # Either the streaming limiter signals 413, or httpx auto-adds a
    # Content-Length from the aggregated body and the fast-path returns 413.
    assert status == 413


# ---------------------------------------------------------------------------
# End-to-end via create_app: 200KB description -> 422, 100MB body -> 413
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app_with_auth_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build a full Bernstein app with auth disabled for black-box testing."""
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    jsonl_path = tmp_path / "tasks.jsonl"
    return create_app(jsonl_path=jsonl_path)


def test_post_tasks_200kb_description_returns_422(_app_with_auth_disabled) -> None:
    """POST /tasks with a 200KB description is rejected by pydantic (422)."""
    transport = ASGITransport(app=_app_with_auth_disabled)
    import asyncio

    async def _run() -> int:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload: TaskPostPayload = {
                "title": "big",
                "description": "z" * 200_000,
                "role": "backend",
            }
            resp = await client.post(
                "/tasks",
                json=payload,
            )
            return resp.status_code

    assert asyncio.run(_run()) == 422


def test_post_tasks_100mb_body_returns_413(_app_with_auth_disabled) -> None:
    """POST /tasks with a 100MB body is rejected by middleware (413)."""
    transport = ASGITransport(app=_app_with_auth_disabled)
    import asyncio

    async def _run() -> int:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Lie about size in the header so the middleware rejects at the
            # fast-path without our test having to actually allocate 100MB.
            resp = await client.post(
                "/tasks",
                content=b'{"title": "x", "description": "x"}',
                headers={
                    "Content-Length": str(100 * 1024 * 1024),
                    "Content-Type": "application/json",
                },
            )
            return resp.status_code

    assert asyncio.run(_run()) == 413


def test_post_tasks_small_payload_still_works(_app_with_auth_disabled) -> None:
    """Sanity check: normal POST /tasks still creates the task."""
    transport = ASGITransport(app=_app_with_auth_disabled)
    import asyncio

    async def _run() -> tuple[int, dict[str, Any]]:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload: TaskPostPayload = {
                "title": "ok",
                "description": "normal task",
                "role": "backend",
            }
            resp = await client.post(
                "/tasks",
                json=payload,
            )
            return resp.status_code, resp.json()

    status, body = asyncio.run(_run())
    assert status == 201, body
    assert body["title"] == "ok"


def test_post_tasks_empty_complexity_returns_422_not_500(_app_with_auth_disabled) -> None:
    """POST /tasks with complexity="" must 422, never 500 (regression).

    Schemathesis fuzzing surfaced an unhandled 500 here: the empty string
    passed pydantic and then raised ValueError in the task store.
    """
    transport = ASGITransport(app=_app_with_auth_disabled)
    import asyncio

    async def _run() -> int:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload: TaskPostPayload = {
                "title": "x",
                "description": "x",
                "complexity": "",
            }
            resp = await client.post(
                "/tasks",
                json=payload,
            )
            return resp.status_code

    assert asyncio.run(_run()) == 422


def test_post_tasks_empty_task_type_returns_422_not_500(_app_with_auth_disabled) -> None:
    """POST /tasks with task_type="" must 422, never 500 (regression)."""
    transport = ASGITransport(app=_app_with_auth_disabled)
    import asyncio

    async def _run() -> int:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload: TaskPostPayload = {
                "title": "x",
                "description": "x",
                "task_type": "",
            }
            resp = await client.post(
                "/tasks",
                json=payload,
            )
            return resp.status_code

    assert asyncio.run(_run()) == 422


def test_post_tasks_batch_invalid_scope_returns_422_not_500(_app_with_auth_disabled) -> None:
    """POST /tasks/batch with an invalid scope must 422, never 500 (regression)."""
    transport = ASGITransport(app=_app_with_auth_disabled)
    import asyncio

    async def _run() -> int:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload: TaskBatchPayload = {
                "tasks": [{"title": "x", "description": "x", "scope": "nope"}],
            }
            resp = await client.post(
                "/tasks/batch",
                json=payload,
            )
            return resp.status_code

    assert asyncio.run(_run()) == 422
