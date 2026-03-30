"""Integration tests for the Jira webhook receiver.

Tests the FastAPI app in integrations/jira_webhook/app.py using
httpx.AsyncClient against the ASGI app directly (no real Jira or
Bernstein server required — external calls are mocked).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_payload(
    key: str = "PROJ-1",
    summary: str = "Fix crash",
    status: str = "To Do",
    priority: str = "Medium",
    labels: list[str] | None = None,
    event: str = "jira:issue_created",
) -> dict[str, Any]:
    return {
        "webhookEvent": event,
        "issue": {
            "key": key,
            "fields": {
                "summary": summary,
                "status": {"name": status},
                "priority": {"name": priority},
                "labels": labels or [],
            },
        },
    }


def _bernstein_response(task_id: str = "abc123") -> dict[str, Any]:
    return {"id": task_id, "status": "open"}


def _mock_bernstein_client(task_id: str = "abc123") -> MagicMock:
    """Return a mock async context manager that simulates the Bernstein HTTP client."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _bernstein_response(task_id)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_class = MagicMock(return_value=mock_client)
    return mock_class


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide required env vars so _Config.validate() passes."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "")
    monkeypatch.setenv("JIRA_PROJECT_FILTER", "")
    monkeypatch.setenv("JIRA_LABEL_FILTER", "")
    monkeypatch.setenv("JIRA_DEFAULT_ROLE", "backend")
    monkeypatch.setenv("BERNSTEIN_URL", "http://127.0.0.1:8052")


@pytest_asyncio.fixture
async def client() -> AsyncClient:  # type: ignore[misc]
    """Yield an httpx AsyncClient wired directly to the ASGI app.

    Reimports the module so monkeypatched env vars take effect in _Config.
    """
    import importlib

    import integrations.jira_webhook.app as mod

    importlib.reload(mod)
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /jira/webhook — authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_no_secret_accepts_all(client: AsyncClient) -> None:
    """When no secret is configured every request is accepted."""
    with patch("integrations.jira_webhook.app.httpx.AsyncClient", _mock_bernstein_client()):
        resp = await client.post("/jira/webhook", json=_issue_payload())
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_webhook_correct_secret_query_param(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = "supersecret"
        mock_cfg.project_filter = frozenset()
        mock_cfg.label_filter = frozenset()
        mock_cfg.jira_base_url = "https://test.atlassian.net"
        mock_cfg.jira_email = "test@example.com"
        mock_cfg.jira_api_token = "tok"
        mock_cfg.default_role = "backend"
        mock_cfg.bernstein_url = "http://127.0.0.1:8052"
        with patch("integrations.jira_webhook.app.httpx.AsyncClient", _mock_bernstein_client()):
            resp = await client.post(
                "/jira/webhook?secret=supersecret",
                json=_issue_payload(),
            )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_webhook_wrong_secret_returns_401(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = "supersecret"
        mock_cfg.project_filter = frozenset()
        mock_cfg.label_filter = frozenset()
        resp = await client.post(
            "/jira/webhook?secret=wrongvalue",
            json=_issue_payload(),
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_bearer_token_accepted(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = "tok123"
        mock_cfg.project_filter = frozenset()
        mock_cfg.label_filter = frozenset()
        mock_cfg.jira_base_url = "https://test.atlassian.net"
        mock_cfg.jira_email = "test@example.com"
        mock_cfg.jira_api_token = "tok"
        mock_cfg.default_role = "backend"
        mock_cfg.bernstein_url = "http://127.0.0.1:8052"
        with patch("integrations.jira_webhook.app.httpx.AsyncClient", _mock_bernstein_client()):
            resp = await client.post(
                "/jira/webhook",
                json=_issue_payload(),
                headers={"Authorization": "Bearer tok123"},
            )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# /jira/webhook — event filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_unsupported_event_ignored(client: AsyncClient) -> None:
    payload = _issue_payload()
    payload["webhookEvent"] = "jira:version_created"
    resp = await client.post("/jira/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert resp.json()["reason"] == "unsupported_event"


@pytest.mark.asyncio
async def test_webhook_issue_updated_creates_task(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app.httpx.AsyncClient", _mock_bernstein_client("xyz")):
        resp = await client.post(
            "/jira/webhook",
            json=_issue_payload(event="jira:issue_updated"),
        )
    assert resp.status_code == 201
    assert resp.json()["task_id"] == "xyz"


@pytest.mark.asyncio
async def test_webhook_terminal_issue_ignored(client: AsyncClient) -> None:
    """Done/Cancelled Jira issues should not produce tasks."""
    resp = await client.post(
        "/jira/webhook",
        json=_issue_payload(status="Done"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert resp.json()["reason"] == "terminal_issue"


@pytest.mark.asyncio
async def test_webhook_missing_issue_ignored(client: AsyncClient) -> None:
    resp = await client.post(
        "/jira/webhook",
        json={"webhookEvent": "jira:issue_created"},
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "no_issue_in_payload"


# ---------------------------------------------------------------------------
# /jira/webhook — project + label filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_project_filter_passes(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = ""
        mock_cfg.project_filter = frozenset({"PROJ", "BACK"})
        mock_cfg.label_filter = frozenset()
        mock_cfg.jira_base_url = "https://test.atlassian.net"
        mock_cfg.jira_email = "test@example.com"
        mock_cfg.jira_api_token = "tok"
        mock_cfg.default_role = "backend"
        mock_cfg.bernstein_url = "http://127.0.0.1:8052"
        with patch("integrations.jira_webhook.app.httpx.AsyncClient", _mock_bernstein_client()):
            resp = await client.post("/jira/webhook", json=_issue_payload(key="PROJ-5"))
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_webhook_project_filter_blocks(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = ""
        mock_cfg.project_filter = frozenset({"PROJ"})
        mock_cfg.label_filter = frozenset()
        resp = await client.post("/jira/webhook", json=_issue_payload(key="OTHER-1"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "filtered"


@pytest.mark.asyncio
async def test_webhook_label_filter_passes(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = ""
        mock_cfg.project_filter = frozenset()
        mock_cfg.label_filter = frozenset({"bernstein"})
        mock_cfg.jira_base_url = "https://test.atlassian.net"
        mock_cfg.jira_email = "test@example.com"
        mock_cfg.jira_api_token = "tok"
        mock_cfg.default_role = "backend"
        mock_cfg.bernstein_url = "http://127.0.0.1:8052"
        with patch("integrations.jira_webhook.app.httpx.AsyncClient", _mock_bernstein_client()):
            resp = await client.post(
                "/jira/webhook",
                json=_issue_payload(labels=["bernstein", "backend"]),
            )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_webhook_label_filter_blocks(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._cfg") as mock_cfg:
        mock_cfg.webhook_secret = ""
        mock_cfg.project_filter = frozenset()
        mock_cfg.label_filter = frozenset({"bernstein"})
        resp = await client.post(
            "/jira/webhook",
            json=_issue_payload(labels=["other-label"]),
        )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "filtered"


# ---------------------------------------------------------------------------
# /jira/webhook — task creation payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_creates_task_with_correct_fields(client: AsyncClient) -> None:
    captured: list[dict[str, Any]] = []

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _bernstein_response("task-999")

    mock_client = AsyncMock()

    async def _capture_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json", {}))
        return mock_response

    mock_client.post = _capture_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("integrations.jira_webhook.app.httpx.AsyncClient", return_value=mock_client):
        resp = await client.post(
            "/jira/webhook",
            json=_issue_payload(key="PROJ-7", summary="Add dark mode"),
        )

    assert resp.status_code == 201
    assert resp.json()["jira_key"] == "PROJ-7"
    assert len(captured) == 1
    task_payload = captured[0]
    assert "[PROJ-7] Add dark mode" in task_payload["title"]
    assert task_payload["external_ref"] == "jira:PROJ-7"


# ---------------------------------------------------------------------------
# /bernstein/task-update — Bernstein → Jira sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_update_transitions_jira_issue(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._get_adapter") as mock_factory:
        mock_adapter = MagicMock()
        mock_adapter.transition_issue.return_value = True
        mock_factory.return_value = mock_adapter

        resp = await client.post(
            "/bernstein/task-update",
            json={"task_id": "abc", "status": "done", "external_ref": "jira:PROJ-10"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "synced"
    assert data["jira_key"] == "PROJ-10"
    mock_adapter.transition_issue.assert_called_once_with("PROJ-10", "Done")


@pytest.mark.asyncio
async def test_task_update_no_matching_transition(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._get_adapter") as mock_factory:
        mock_adapter = MagicMock()
        mock_adapter.transition_issue.return_value = False
        mock_factory.return_value = mock_adapter

        resp = await client.post(
            "/bernstein/task-update",
            json={"task_id": "abc", "status": "done", "external_ref": "jira:PROJ-10"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_transition"


@pytest.mark.asyncio
async def test_task_update_non_jira_ref_ignored(client: AsyncClient) -> None:
    resp = await client.post(
        "/bernstein/task-update",
        json={"task_id": "abc", "status": "done", "external_ref": "linear:ENG-5"},
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "not_a_jira_task"


@pytest.mark.asyncio
async def test_task_update_unknown_status_returns_400(client: AsyncClient) -> None:
    resp = await client.post(
        "/bernstein/task-update",
        json={"task_id": "abc", "status": "not_a_real_status", "external_ref": "jira:PROJ-1"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_task_update_in_progress_maps_to_jira(client: AsyncClient) -> None:
    with patch("integrations.jira_webhook.app._get_adapter") as mock_factory:
        mock_adapter = MagicMock()
        mock_adapter.transition_issue.return_value = True
        mock_factory.return_value = mock_adapter

        resp = await client.post(
            "/bernstein/task-update",
            json={"task_id": "abc", "status": "in_progress", "external_ref": "jira:PROJ-2"},
        )

    assert resp.status_code == 200
    mock_adapter.transition_issue.assert_called_once_with("PROJ-2", "In Progress")
