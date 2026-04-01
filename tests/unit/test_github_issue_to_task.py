"""Unit tests for GitHub issue → Bernstein task conversion via the 'bernstein' label.

Tests cover trigger_label_to_task() mapper function and the full webhook
endpoint path: issue labeled 'bernstein' → task created in the store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.github_app.mapper import TRIGGER_LABELS, trigger_label_to_task
from bernstein.github_app.webhooks import WebhookEvent

if TYPE_CHECKING:
    from pathlib import Path

WEBHOOK_SECRET = "test-secret-for-issue-conversion"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _labeled_payload(
    label_name: str = "bernstein",
    number: int = 55,
    title: str = "Add rate-limiting to the API",
    body: str = "The API has no rate limiting and gets hammered during peak hours.",
    all_labels: list[str] | None = None,
    sender: str = "octocat",
) -> dict[str, Any]:
    """Build a synthetic GitHub issues 'labeled' webhook payload."""
    if all_labels is None:
        all_labels = [label_name]
    return {
        "action": "labeled",
        "label": {"name": label_name},
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": lbl} for lbl in all_labels],
        },
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": sender},
    }


def _make_event(payload: dict[str, Any], action: str = "labeled") -> WebhookEvent:
    return WebhookEvent(
        event_type="issues",
        action=action,
        repo_full_name="acme/widgets",
        sender=payload.get("sender", {}).get("login", "octocat"),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TRIGGER_LABELS constant
# ---------------------------------------------------------------------------


class TestTriggerLabels:
    def test_bernstein_in_trigger_labels(self) -> None:
        assert "bernstein" in TRIGGER_LABELS

    def test_agent_fix_in_trigger_labels(self) -> None:
        assert "agent-fix" in TRIGGER_LABELS

    def test_agent_task_in_trigger_labels(self) -> None:
        assert "agent-task" in TRIGGER_LABELS


# ---------------------------------------------------------------------------
# trigger_label_to_task — bernstein label
# ---------------------------------------------------------------------------


class TestTriggerLabelToTask:
    def test_bernstein_label_creates_task(self) -> None:
        payload = _labeled_payload(label_name="bernstein")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None

    def test_bernstein_task_title_contains_issue_number(self) -> None:
        payload = _labeled_payload(label_name="bernstein", number=55, title="Add rate-limiting")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert "[GH#55]" in task["title"]
        assert "Add rate-limiting" in task["title"]

    def test_bernstein_task_type_is_standard(self) -> None:
        payload = _labeled_payload(label_name="bernstein")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["task_type"] == "standard"

    def test_bernstein_description_mentions_label(self) -> None:
        payload = _labeled_payload(label_name="bernstein", sender="alice")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert "bernstein" in task["description"]
        assert "@alice" in task["description"]

    def test_bernstein_description_contains_body(self) -> None:
        body_text = "The API has no rate limiting and gets hammered during peak hours."
        payload = _labeled_payload(label_name="bernstein", body=body_text)
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert body_text[:50] in task["description"]

    def test_bernstein_default_role_is_backend(self) -> None:
        payload = _labeled_payload(label_name="bernstein")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["role"] == "backend"

    def test_bernstein_default_priority_is_2(self) -> None:
        payload = _labeled_payload(label_name="bernstein")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["priority"] == 2

    def test_bernstein_bug_label_raises_priority_to_1(self) -> None:
        # "bug" must come before "bernstein" — _priority_from_labels returns on first match
        payload = _labeled_payload(label_name="bernstein", all_labels=["bug", "bernstein"])
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["priority"] == 1

    def test_bernstein_docs_label_sets_role(self) -> None:
        payload = _labeled_payload(label_name="bernstein", all_labels=["bernstein", "docs"])
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["role"] == "docs"

    def test_bernstein_qa_label_sets_role(self) -> None:
        payload = _labeled_payload(label_name="bernstein", all_labels=["bernstein", "qa"])
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["role"] == "qa"

    def test_bernstein_short_body_gives_small_scope(self) -> None:
        payload = _labeled_payload(label_name="bernstein", body="Short body.")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["scope"] == "small"

    def test_bernstein_medium_body_gives_medium_scope(self) -> None:
        medium_body = "x" * 500
        payload = _labeled_payload(label_name="bernstein", body=medium_body)
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["scope"] == "medium"

    def test_bernstein_long_body_gives_large_scope(self) -> None:
        large_body = "x" * 1500
        payload = _labeled_payload(label_name="bernstein", body=large_body)
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["scope"] == "large"

    def test_bernstein_title_capped_at_120_chars(self) -> None:
        long_title = "A" * 200
        payload = _labeled_payload(label_name="bernstein", title=long_title)
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert len(task["title"]) <= 120

    def test_non_trigger_label_returns_none(self) -> None:
        payload = _labeled_payload(label_name="wontfix")
        event = _make_event(payload)
        assert trigger_label_to_task(event) is None

    def test_unlabeled_action_returns_none(self) -> None:
        payload = _labeled_payload(label_name="bernstein")
        event = _make_event(payload, action="unlabeled")
        assert trigger_label_to_task(event) is None

    # ------------------------------------------------------------------
    # agent-fix label
    # ------------------------------------------------------------------

    def test_agent_fix_label_creates_fix_task(self) -> None:
        payload = _labeled_payload(label_name="agent-fix")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["task_type"] == "fix"

    def test_agent_fix_priority_is_1(self) -> None:
        payload = _labeled_payload(label_name="agent-fix")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["priority"] == 1

    # ------------------------------------------------------------------
    # agent-task label
    # ------------------------------------------------------------------

    def test_agent_task_label_creates_standard_task(self) -> None:
        payload = _labeled_payload(label_name="agent-task")
        event = _make_event(payload)
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["task_type"] == "standard"


# ---------------------------------------------------------------------------
# Webhook endpoint integration — bernstein label → task in store
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bernstein_label_webhook_creates_task(client: AsyncClient) -> None:
    """POST /webhooks/github with bernstein label event creates a task."""
    payload = _labeled_payload(label_name="bernstein", number=101, title="Improve error output")
    body = json.dumps(payload).encode()
    sig = _sign(body)

    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_type"] == "issues"
    assert data["action"] == "labeled"
    assert data["tasks_created"] == 1


@pytest.mark.anyio
async def test_bernstein_label_webhook_task_retrievable(client: AsyncClient) -> None:
    """Task created via bernstein label webhook is retrievable from the store."""
    payload = _labeled_payload(label_name="bernstein", number=202, title="Add retries to HTTP client")
    body = json.dumps(payload).encode()
    sig = _sign(body)

    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )
    task_ids = resp.json()["task_ids"]
    assert len(task_ids) == 1

    task_resp = await client.get(f"/tasks/{task_ids[0]}")
    assert task_resp.status_code == 200
    task_data = task_resp.json()
    assert "[GH#202]" in task_data["title"]
    assert task_data["status"] == "open"


@pytest.mark.anyio
async def test_agent_fix_label_webhook_creates_fix_task(client: AsyncClient) -> None:
    """POST /webhooks/github with agent-fix label creates a fix task."""
    payload = _labeled_payload(label_name="agent-fix", number=303, title="Fix null pointer crash")
    body = json.dumps(payload).encode()
    sig = _sign(body)

    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["tasks_created"] == 1
    task_id = resp.json()["task_ids"][0]

    task_resp = await client.get(f"/tasks/{task_id}")
    assert task_resp.status_code == 200
    assert task_resp.json()["task_type"] == "fix"


@pytest.mark.anyio
async def test_non_trigger_label_webhook_creates_no_task(client: AsyncClient) -> None:
    """POST /webhooks/github with a non-trigger label (not bernstein/agent-fix/agent-task
    and not evolve-candidate) creates no tasks."""
    payload = _labeled_payload(label_name="wontfix", number=404, title="Some issue")
    body = json.dumps(payload).encode()
    sig = _sign(body)

    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["tasks_created"] == 0
