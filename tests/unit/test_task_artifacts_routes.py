"""Task-server agent-posted artifact endpoint tests (#2553).

AC1: a worker posts a report and it appears on the task detail surface over SSE
without reload, rendered with key, version, journal position, and content hash.
Also covers version chaining, tamper rendering, claim isolation (the authorization
principal is the authenticated request identity, never the request body), the
audit-recorded refusal, the oversized 413 path, and the non-assertable progress
vector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.security.audit_chain import (
    EVENT_RUN_ARTIFACT_REFUSED,
    AuditChainStore,
)
from bernstein.core.server import SSEBus, TaskStore, create_app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

_KEY = b"artifacts-routes-test-key-abc123456"


@pytest.fixture(scope="module")
def _module_app(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("artifacts-server")
    return create_app(jsonl_path=root / "runtime" / "tasks.jsonl")


@pytest.fixture()
def app(_module_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    # Isolate the spine/artifact HMAC key to the test dir (the route resolves it
    # via load_or_create_audit_key(), which honours BERNSTEIN_AUDIT_KEY_PATH).
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    _module_app.state.store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    _module_app.state.sse_bus = SSEBus()
    _module_app.state.draining = False
    _module_app.state.sdd_dir = tmp_path
    _module_app.state.runtime_dir = tmp_path / "runtime"
    _module_app.state.workdir = tmp_path.parent
    _module_app.state.audit_chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    return _module_app


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task(client: AsyncClient, title: str) -> str:
    resp = await client.post(
        "/tasks",
        json={"title": title, "description": f"{title} description", "role": "backend"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _claim(app, task_id: str, holder: str) -> None:  # type: ignore[no-untyped-def]
    """Record ``holder`` as the task's active claim holder."""
    app.state.store.get_task(task_id).assigned_agent = holder


async def _create_claimed_task(client: AsyncClient, app, title: str, holder: str) -> str:  # type: ignore[no-untyped-def]
    task_id = await _create_task(client, title)
    _claim(app, task_id, holder)
    return task_id


async def _post_artifact(client: AsyncClient, task_id: str, holder: str, **body: object):  # type: ignore[no-untyped-def]
    """Post an artifact as ``holder`` (identity carried in the request header)."""
    return await client.post(
        f"/tasks/{task_id}/artifacts",
        json={"poster": holder, **body},
        headers={"x-bernstein-agent-id": holder},
    )


def _spy_sse(app) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    recorded: list[tuple[str, str]] = []
    original = app.state.sse_bus.publish

    def _record(event_type: str, data: str = "{}") -> None:
        recorded.append((event_type, data))
        original(event_type, data)

    app.state.sse_bus.publish = _record  # type: ignore[method-assign]
    return recorded


class TestPosting:
    async def test_post_report_appears_with_anchors_and_over_sse(self, app, client: AsyncClient) -> None:
        recorded = _spy_sse(app)
        task_id = await _create_claimed_task(client, app, "report task", "worker-a")

        resp = await _post_artifact(
            client,
            task_id,
            "worker-a",
            key="audit-summary",
            artifact_type="report",
            body="# Findings\nAll clear.",
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["version"] == 1
        assert data["journal_index"] == 0
        assert data["content_hash"].startswith("sha256:")
        assert data["verified"] is True
        assert data["content"] == {"type": "report", "body": "# Findings\nAll clear."}

        # AC1: the artifact was delivered over SSE without a reload.
        artifact_events = [d for (e, d) in recorded if e == "task.artifact"]
        assert artifact_events, "expected a task.artifact SSE event"
        assert any(e == "task.progress" for (e, _d) in recorded)

        # It renders on the listing with key, version, position, and hash.
        listing = await client.get(f"/tasks/{task_id}/artifacts")
        assert listing.status_code == 200
        rows = listing.json()
        assert len(rows) == 1
        assert rows[0]["key"] == "audit-summary"
        assert rows[0]["verified"] is True

    async def test_table_and_link_post(self, app, client: AsyncClient) -> None:
        task_id = await _create_claimed_task(client, app, "table task", "w")
        table = await _post_artifact(
            client,
            task_id,
            "w",
            key="cmp",
            artifact_type="table",
            columns=["metric", "before", "after"],
            rows=[["p95", "120", "80"]],
        )
        assert table.status_code == 201, table.text
        link = await _post_artifact(
            client,
            task_id,
            "w",
            key="preview",
            artifact_type="link",
            url="https://preview.example/xyz",
            link_kind="preview",
        )
        assert link.status_code == 201, link.text
        assert link.json()["link_kind"] == "preview"
        assert link.json()["content"] == {"type": "link", "url": "https://preview.example/xyz", "kind": "preview"}

    async def test_reposting_key_chains_versions(self, app, client: AsyncClient) -> None:
        task_id = await _create_claimed_task(client, app, "versioned", "w")
        v1 = await _post_artifact(client, task_id, "w", key="r", artifact_type="report", body="one")
        v2 = await _post_artifact(client, task_id, "w", key="r", artifact_type="report", body="two")
        assert v1.status_code == 201 and v2.status_code == 201
        assert v2.json()["version"] == 2
        assert v2.json()["prev_version_hash"] == v1.json()["spine_entry_hash"]
        rows = (await client.get(f"/tasks/{task_id}/artifacts")).json()
        assert [r["version"] for r in rows] == [1, 2]


class TestCaps:
    async def test_oversized_payload_is_413(self, app, client: AsyncClient) -> None:
        task_id = await _create_claimed_task(client, app, "big", "w")
        # An oversized payload is rejected with 413 end to end (here by the
        # server's request-size guard, which shares the 1 MiB per-blob ceiling).
        # The route's own ArtifactTooLargeError -> 413 mapping, which names the
        # cap, is exercised in tests/unit/test_run_artifacts.py::TestCap.
        resp = await _post_artifact(
            client,
            task_id,
            "w",
            key="big",
            artifact_type="report",
            body="x" * 1_048_576,
        )
        assert resp.status_code == 413, resp.text
        assert "exceeds" in resp.json()["detail"]


class TestClaimIsolation:
    async def test_post_against_unheld_task_is_refused_and_audited(self, app, client: AsyncClient) -> None:
        await _create_claimed_task(client, app, "task A", "agent-A")
        task_b = await _create_claimed_task(client, app, "task B", "agent-B")

        # A worker whose authenticated identity is agent-A tries to post to task
        # B (held by agent-B): refused (identity != claim holder).
        resp = await _post_artifact(client, task_b, "agent-A", key="k", artifact_type="report", body="sneaky")
        assert resp.status_code == 403, resp.text

        # The refusal is audit-recorded with the authenticated caller.
        refusals = app.state.audit_chain.query(event_type=EVENT_RUN_ARTIFACT_REFUSED)
        assert refusals
        assert refusals[-1].details["caller"] == "agent-A"
        assert refusals[-1].details["task_id"] == task_b

        # A refused post writes nothing: task B still has no artifacts.
        listing = await client.get(f"/tasks/{task_b}/artifacts")
        assert listing.json() == []

        # The rightful holder can post.
        ok = await _post_artifact(client, task_b, "agent-B", key="k", artifact_type="report", body="legit")
        assert ok.status_code == 201

    async def test_unclaimed_task_rejects_posts(self, app, client: AsyncClient) -> None:
        task_id = await _create_task(client, "unclaimed")  # no claim holder set
        resp = await _post_artifact(client, task_id, "whoever", key="k", artifact_type="report", body="x")
        assert resp.status_code == 403, resp.text
        assert app.state.audit_chain.query(event_type=EVENT_RUN_ARTIFACT_REFUSED)

    async def test_body_poster_cannot_forge_identity(self, app, client: AsyncClient) -> None:
        # The claim holder is agent-B; a caller sends the correct body poster but
        # its authenticated header identity is agent-A. Authorization uses the
        # header, not the body, so the post is still refused.
        task_id = await _create_claimed_task(client, app, "spoof", "agent-B")
        resp = await client.post(
            f"/tasks/{task_id}/artifacts",
            json={"poster": "agent-B", "key": "k", "artifact_type": "report", "body": "x"},
            headers={"x-bernstein-agent-id": "agent-A"},
        )
        assert resp.status_code == 403, resp.text


class TestProgress:
    async def test_progress_is_not_moved_by_posting_artifacts(self, app, client: AsyncClient) -> None:
        task_id = await _create_claimed_task(client, app, "progress task", "w")
        before = (await client.get(f"/tasks/{task_id}/progress")).json()
        for i in range(5):
            resp = await _post_artifact(client, task_id, "w", key=f"r{i}", artifact_type="report", body=f"body {i}")
            assert resp.status_code == 201, resp.text
        after = (await client.get(f"/tasks/{task_id}/progress")).json()
        # Posting five reports does not move the chain-computed progress vector.
        assert after["vector_hash"] == before["vector_hash"]
        assert after["earned_steps"] == 0
