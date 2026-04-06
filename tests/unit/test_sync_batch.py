"""Tests for batch sync in sync_backlog_to_server().

Validates that Step 1 (task creation) uses the ``POST /tasks/batch``
endpoint, falls back to one-by-one on 404, and handles errors correctly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from bernstein.core.sync import SyncResult, sync_backlog_to_server

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_MD_A = """\
# Task Alpha

**Role:** backend
**Priority:** 2 (normal)
**Scope:** medium
**Complexity:** medium

Alpha description.
"""

SAMPLE_MD_B = """\
# Task Beta

**Role:** frontend
**Priority:** 1 (critical)
**Scope:** small
**Complexity:** low

Beta description.
"""

SAMPLE_MD_C = """\
# Task Gamma

**Role:** qa
**Priority:** 3 (nice-to-have)
**Scope:** large
**Complexity:** high

Gamma description.
"""

_Handler = Callable[[httpx.Request], httpx.Response]


def _write_md(path: Path, content: str) -> None:
    """Write content to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _status_responses() -> dict[str, httpx.Response]:
    """Standard empty-server responses for all status queries."""
    return {
        "GET /tasks?status=open": httpx.Response(200, json=[]),
        "GET /tasks?status=claimed": httpx.Response(200, json=[]),
        "GET /tasks?status=in_progress": httpx.Response(200, json=[]),
        "GET /tasks?status=done": httpx.Response(200, json=[]),
        "GET /tasks?status=failed": httpx.Response(200, json=[]),
        "GET /tasks?status=closed": httpx.Response(200, json=[]),
    }


def _make_handler(
    post_hook: _Handler | None = None,
    *,
    extra_status: dict[str, httpx.Response] | None = None,
) -> _Handler:
    """Build a mock handler with standard status responses and optional POST hook.

    Args:
        post_hook: If set, called for every POST request. Return a response
            to handle it, or ``None`` to fall through to the default 404.
        extra_status: Additional or overridden status-query responses merged
            on top of the empty-server defaults.
    """
    responses = _status_responses()
    if extra_status:
        responses.update(extra_status)

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"

        if request.method == "POST" and post_hook is not None:
            result = post_hook(request)
            if result is not None:
                return result

        resp = responses.get(key)
        if resp is not None:
            return resp
        return httpx.Response(404, json={"detail": f"No mock for {key}"})

    return handler


def _sync(tmp_path: Path, handler: _Handler) -> SyncResult:
    """Run sync_backlog_to_server with the given mock handler."""
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    return sync_backlog_to_server(tmp_path, "http://test", client=client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sync_batch_creates_all_tasks(tmp_path: Path) -> None:
    """Batch endpoint returns 3 created tasks; result.created has 3 IDs."""
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    _write_md(backlog_open / "001-alpha.md", SAMPLE_MD_A)
    _write_md(backlog_open / "002-beta.md", SAMPLE_MD_B)
    _write_md(backlog_open / "003-gamma.md", SAMPLE_MD_C)

    captured_batch: list[dict[str, object]] = []

    def post_hook(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks/batch":
            body = json.loads(request.content)
            captured_batch.append(body)
            created = [{"id": f"T-{i + 1:03d}", "title": t["title"]} for i, t in enumerate(body["tasks"])]
            return httpx.Response(201, json={"created": created, "skipped_titles": []})
        return httpx.Response(404, json={"detail": "no mock"})

    result = _sync(tmp_path, _make_handler(post_hook))

    assert len(result.created) == 3
    assert result.created == ["T-001", "T-002", "T-003"]
    assert result.errors == []
    assert len(captured_batch) == 1
    assert len(captured_batch[0]["tasks"]) == 3


def test_sync_batch_skips_duplicates(tmp_path: Path) -> None:
    """Files whose titles already exist on the server are skipped pre-batch."""
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    _write_md(backlog_open / "001-alpha.md", SAMPLE_MD_A)
    _write_md(backlog_open / "002-beta.md", SAMPLE_MD_B)

    existing_task = {"id": "T-existing", "title": "Task Alpha", "status": "open"}
    captured_batch: list[dict[str, object]] = []

    def post_hook(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks/batch":
            body = json.loads(request.content)
            captured_batch.append(body)
            created = [{"id": "T-new-1", "title": body["tasks"][0]["title"]}]
            return httpx.Response(201, json={"created": created, "skipped_titles": []})
        return httpx.Response(404, json={"detail": "no mock"})

    result = _sync(
        tmp_path,
        _make_handler(
            post_hook,
            extra_status={
                "GET /tasks?status=open": httpx.Response(200, json=[existing_task]),
            },
        ),
    )

    assert "001-alpha.md" in result.skipped
    assert len(result.created) == 1
    assert len(captured_batch) == 1
    assert len(captured_batch[0]["tasks"]) == 1
    assert captured_batch[0]["tasks"][0]["title"] == "Task Beta"


def test_sync_batch_fallback_on_404(tmp_path: Path) -> None:
    """When /tasks/batch returns 404, fallback creates tasks one-by-one."""
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    _write_md(backlog_open / "001-alpha.md", SAMPLE_MD_A)
    _write_md(backlog_open / "002-beta.md", SAMPLE_MD_B)

    individual_posts: list[dict[str, object]] = []

    def post_hook(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks/batch":
            return httpx.Response(404, json={"detail": "not found"})
        if request.url.path == "/tasks":
            body = json.loads(request.content)
            individual_posts.append(body)
            task_id = f"T-{len(individual_posts):03d}"
            return httpx.Response(201, json={"id": task_id, "status": "open"})
        return httpx.Response(404, json={"detail": "no mock"})

    result = _sync(tmp_path, _make_handler(post_hook))

    assert len(result.created) == 2
    assert result.errors == []
    assert len(individual_posts) == 2


def test_sync_batch_records_errors_on_failure(tmp_path: Path) -> None:
    """A 500 from /tasks/batch records an error without creating any tasks."""
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    _write_md(backlog_open / "001-alpha.md", SAMPLE_MD_A)

    def post_hook(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks/batch":
            return httpx.Response(500, json={"detail": "internal error"})
        return httpx.Response(404, json={"detail": "no mock"})

    result = _sync(tmp_path, _make_handler(post_hook))

    assert result.created == []
    assert len(result.errors) == 1
    assert "Batch create failed" in result.errors[0]


def test_sync_batch_handles_empty_backlog(tmp_path: Path) -> None:
    """No files in open/ means no HTTP calls to /tasks or /tasks/batch."""
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    backlog_open.mkdir(parents=True)

    post_calls: list[str] = []

    def post_hook(request: httpx.Request) -> httpx.Response:
        post_calls.append(request.url.path)
        return httpx.Response(404, json={"detail": "no mock"})

    result = _sync(tmp_path, _make_handler(post_hook))

    assert result == SyncResult()
    assert post_calls == []
