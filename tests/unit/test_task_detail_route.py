"""WEB-012: Tests for dashboard task detail view and log streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from threading import get_ident
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.routes import task_detail
from bernstein.core.server import create_app
from bernstein.core.tasks.models import TaskStatus


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestTaskDetail:
    """Test GET /dashboard/tasks/{task_id}."""

    @pytest.mark.anyio()
    async def test_detail_not_found(self, client: AsyncClient) -> None:
        """Non-existent task should return 404."""
        resp = await client.get("/dashboard/tasks/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.anyio()
    async def test_detail_exists(self, client: AsyncClient) -> None:
        """Created task should be viewable in detail."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Detail test", "description": "Testing detail view", "role": "backend"},
        )
        assert create_resp.status_code == 201
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/dashboard/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"]["id"] == task_id
        assert data["task"]["title"] == "Detail test"
        assert "log_tail" in data
        assert "log_size" in data

    @pytest.mark.anyio()
    async def test_detail_includes_progress(self, client: AsyncClient) -> None:
        """Detail response should include progress entries."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Progress test", "description": "With progress", "role": "backend"},
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/dashboard/tasks/{task_id}")
        data = resp.json()
        assert "progress_entries" in data
        assert isinstance(data["progress_entries"], list)


class TestTaskLogStream:
    """Test GET /dashboard/tasks/{task_id}/logs/stream."""

    @pytest.mark.anyio()
    async def test_stream_not_found(self, client: AsyncClient) -> None:
        """Non-existent task should return 404."""
        resp = await client.get("/dashboard/tasks/nonexistent/logs/stream")
        assert resp.status_code == 404

    @pytest.mark.anyio()
    async def test_stream_returns_sse(self, client: AsyncClient) -> None:
        """Stream endpoint should return text/event-stream."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Stream test", "description": "SSE stream", "role": "backend"},
        )
        task_id = create_resp.json()["id"]

        # Complete the task so the stream ends quickly
        await client.post(f"/tasks/{task_id}/claim", json={"agent_id": "test-agent"})
        await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "done"})

        resp = await client.get(f"/dashboard/tasks/{task_id}/logs/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert resp.text == 'event: complete\ndata: {"status": "done"}\n\nevent: close\ndata: {}\n\n'

    @pytest.mark.asyncio
    async def test_running_stream_reads_off_loop(
        self, app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/tasks", json={"title": "Synthetic stream", "description": "test", "role": "backend"}
            )
            task_id = created.json()["id"]
            store = app.state.store
            running = replace(store.get_task(task_id), status=TaskStatus.IN_PROGRESS, assigned_agent="synthetic-agent")
            done = replace(running, status=TaskStatus.DONE)
            monkeypatch.setattr(store, "get_task", Mock(side_effect=[running, running, running, running, done]))
            monkeypatch.setattr(app.state, "runtime_dir", tmp_path)
            monkeypatch.setattr(task_detail, "_POLL_INTERVAL", 0)

            # Exercise the real open/seek/read callback with a synthetic log and append.
            log = tmp_path / "synthetic.log"
            log.write_text("one\ntwo\n", encoding="utf-8")
            loop_thread = get_ident()
            reads: list[tuple[int, int]] = []

            def read_log(
                session_id: str, directory: Path, reader: Callable[[Path, int], tuple[str, int] | None], offset: int
            ) -> tuple[str, int] | None:
                assert (session_id, directory) == ("synthetic-agent", tmp_path)
                reads.append((get_ident(), offset))
                if len(reads) == 3:
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write("three\n")
                return reader(log, offset)

            monkeypatch.setattr(task_detail, "_try_read_session_log", read_log)
            resp = await client.get(f"/dashboard/tasks/{task_id}/logs/stream")
            events = resp.text.split("\n\n")
            assert events[:2] == ["event: log\ndata: one", "event: log\ndata: two"]
            assert events[2].startswith('event: ping\ndata: {"ts": ')
            assert events[3:] == [
                "event: log\ndata: three",
                'event: complete\ndata: {"status": "done"}',
                "event: close\ndata: {}",
                "",
            ]
            assert [offset for _, offset in reads] == [0, 8, 8]
            assert all(thread != loop_thread for thread, _ in reads)


class TestTaskDiff:
    """Test GET /dashboard/tasks/{task_id}/diff."""

    @pytest.mark.anyio()
    async def test_diff_not_found(self, client: AsyncClient) -> None:
        """Non-existent task should return 404."""
        resp = await client.get("/dashboard/tasks/nonexistent/diff")
        assert resp.status_code == 404

    @pytest.mark.anyio()
    async def test_diff_shape_when_no_branch(self, client: AsyncClient) -> None:
        """Unassigned task should still return a structured diff payload."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Diff test", "description": "Diff", "role": "backend"},
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/dashboard/tasks/{task_id}/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == task_id
        assert "branch" in data
        assert "base_ref" in data
        assert "files" in data
        assert isinstance(data["files"], list)
        assert isinstance(data["additions"], int)
        assert isinstance(data["deletions"], int)
        assert "unified" in data
        assert "generated_at" in data

    @pytest.mark.anyio()
    async def test_diff_api_v1_alias(self, client: AsyncClient) -> None:
        """Endpoint should be reachable via the /api/v1 prefix too."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Diff v1", "description": "Diff", "role": "backend"},
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/api/v1/dashboard/tasks/{task_id}/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == task_id


class TestDiffParser:
    """Exercise the unified-diff parser directly."""

    def test_parse_simple(self) -> None:
        from bernstein.core.routes.task_detail import _parse_unified_diff

        text = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "-line2\n"
            "+line2-changed\n"
            "+line2b\n"
            " line3\n"
        )
        files = _parse_unified_diff(text)
        assert len(files) == 1
        f = files[0]
        assert f.path == "foo.py"
        assert f.language == "python"
        assert f.additions == 2
        assert f.deletions == 1
        assert len(f.hunks) == 1
        assert f.hunks[0].old_start == 1
        assert f.hunks[0].new_start == 1

    def test_parse_new_and_deleted(self) -> None:
        from bernstein.core.routes.task_detail import _parse_unified_diff

        text = (
            "diff --git a/added.ts b/added.ts\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/added.ts\n"
            "@@ -0,0 +1,2 @@\n"
            "+a\n"
            "+b\n"
            "diff --git a/gone.md b/gone.md\n"
            "deleted file mode 100644\n"
            "--- a/gone.md\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-removed\n"
        )
        files = _parse_unified_diff(text)
        assert len(files) == 2
        added = next(f for f in files if f.path == "added.ts")
        deleted = next(f for f in files if f.path == "gone.md")
        assert added.status == "added"
        assert added.additions == 2
        assert added.language == "ts"
        assert deleted.status == "deleted"
        assert deleted.deletions == 1
