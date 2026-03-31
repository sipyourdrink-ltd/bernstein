"""Tests for the GitHub App webhook integration."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.github_app.app import GitHubAppConfig, _base64url_encode, _build_jwt_parts
from bernstein.github_app.mapper import (
    issue_to_tasks,
    label_to_action,
    pr_review_to_task,
    push_to_tasks,
)
from bernstein.github_app.webhooks import WebhookEvent, parse_webhook, verify_signature

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-secret-key-for-hmac"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Compute sha256 HMAC signature in GitHub format."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _issue_payload(
    number: int = 42,
    title: str = "Fix the parser",
    body: str = "The JSON parser crashes on nested arrays.",
    labels: list[dict[str, str]] | None = None,
    action: str = "opened",
) -> dict[str, Any]:
    """Build a synthetic GitHub issues webhook payload."""
    if labels is None:
        labels = []
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "labels": labels,
        },
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "octocat"},
    }


def _pr_review_comment_payload(
    pr_number: int = 10,
    pr_title: str = "Add caching layer",
    comment_body: str = "You should fix the race condition here.",
    path: str = "src/bernstein/core/cache.py",
) -> dict[str, Any]:
    """Build a synthetic PR review comment payload."""
    return {
        "action": "created",
        "comment": {
            "body": comment_body,
            "path": path,
        },
        "pull_request": {
            "number": pr_number,
            "title": pr_title,
        },
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "reviewer"},
    }


def _push_payload(
    ref: str = "refs/heads/main",
    commits: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a synthetic push event payload."""
    if commits is None:
        commits = [
            {"id": "abc12345", "message": "feat: add caching"},
            {"id": "def67890", "message": "test: add cache tests"},
        ]
    return {
        "ref": ref,
        "commits": commits,
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "pusher"},
    }


def _label_payload(
    label_name: str = "evolve-candidate",
    number: int = 99,
    title: str = "Improve error messages",
) -> dict[str, Any]:
    """Build a synthetic label event payload."""
    return {
        "action": "labeled",
        "label": {"name": label_name},
        "issue": {
            "number": number,
            "title": title,
            "body": "Error messages are unclear.",
            "labels": [{"name": label_name}],
        },
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "labeler"},
    }


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
# verify_signature tests
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_valid_signature_passes(self) -> None:
        body = b'{"action":"opened"}'
        sig = _sign(body)
        assert verify_signature(body, sig, WEBHOOK_SECRET) is True

    def test_invalid_signature_fails(self) -> None:
        body = b'{"action":"opened"}'
        assert verify_signature(body, "sha256=badhex", WEBHOOK_SECRET) is False

    def test_missing_prefix_fails(self) -> None:
        body = b'{"action":"opened"}'
        digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        # Missing "sha256=" prefix
        assert verify_signature(body, digest, WEBHOOK_SECRET) is False

    def test_wrong_secret_fails(self) -> None:
        body = b'{"action":"opened"}'
        sig = _sign(body, "wrong-secret")
        assert verify_signature(body, sig, WEBHOOK_SECRET) is False

    def test_empty_body(self) -> None:
        body = b""
        sig = _sign(body)
        assert verify_signature(body, sig, WEBHOOK_SECRET) is True


# ---------------------------------------------------------------------------
# parse_webhook tests
# ---------------------------------------------------------------------------


class TestParseWebhook:
    def test_issues_opened(self) -> None:
        payload = _issue_payload()
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "issues"}
        event = parse_webhook(headers, body)
        assert event.event_type == "issues"
        assert event.action == "opened"
        assert event.repo_full_name == "acme/widgets"
        assert event.sender == "octocat"

    def test_pull_request_opened(self) -> None:
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 5,
                "title": "Add feature X",
            },
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "dev"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "pull_request"}
        event = parse_webhook(headers, body)
        assert event.event_type == "pull_request"
        assert event.action == "opened"
        assert event.repo_full_name == "acme/widgets"

    def test_push_event(self) -> None:
        payload = _push_payload()
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "push"}
        event = parse_webhook(headers, body)
        assert event.event_type == "push"
        assert event.action == ""  # push has no action field
        assert event.repo_full_name == "acme/widgets"
        assert event.sender == "pusher"

    def test_missing_event_header_raises(self) -> None:
        body = json.dumps(_issue_payload()).encode()
        with pytest.raises(ValueError, match="Missing X-GitHub-Event"):
            parse_webhook({}, body)

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_webhook({"X-GitHub-Event": "issues"}, b"not json{{{")

    def test_missing_repo_raises(self) -> None:
        payload = {"action": "opened", "sender": {"login": "x"}}
        body = json.dumps(payload).encode()
        with pytest.raises(ValueError, match=r"Missing repository\.full_name"):
            parse_webhook({"X-GitHub-Event": "issues"}, body)

    def test_case_insensitive_headers(self) -> None:
        payload = _issue_payload()
        body = json.dumps(payload).encode()
        # lowercase header key
        headers = {"x-github-event": "issues"}
        event = parse_webhook(headers, body)
        assert event.event_type == "issues"


# ---------------------------------------------------------------------------
# issue_to_tasks tests
# ---------------------------------------------------------------------------


class TestIssueToTasks:
    def test_bug_issue_priority_1(self) -> None:
        payload = _issue_payload(labels=[{"name": "bug"}])
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        tasks = issue_to_tasks(event)
        assert len(tasks) == 1
        assert tasks[0]["priority"] == 1

    def test_enhancement_issue_priority_2(self) -> None:
        payload = _issue_payload(labels=[{"name": "enhancement"}])
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        tasks = issue_to_tasks(event)
        assert len(tasks) == 1
        assert tasks[0]["priority"] == 2

    def test_backend_label_sets_role(self) -> None:
        payload = _issue_payload(labels=[{"name": "backend"}, {"name": "enhancement"}])
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        tasks = issue_to_tasks(event)
        assert len(tasks) == 1
        assert tasks[0]["role"] == "backend"

    def test_docs_label_sets_role(self) -> None:
        payload = _issue_payload(labels=[{"name": "docs"}])
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        tasks = issue_to_tasks(event)
        assert len(tasks) == 1
        assert tasks[0]["role"] == "docs"
        assert tasks[0]["priority"] == 3

    def test_no_labels_defaults(self) -> None:
        payload = _issue_payload(labels=[])
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        tasks = issue_to_tasks(event)
        assert len(tasks) == 1
        assert tasks[0]["priority"] == 2
        assert tasks[0]["role"] == "backend"

    def test_wrong_event_type_returns_empty(self) -> None:
        event = WebhookEvent(
            event_type="push",
            action="",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload={},
        )
        assert issue_to_tasks(event) == []

    def test_closed_action_returns_empty(self) -> None:
        payload = _issue_payload(action="closed")
        event = WebhookEvent(
            event_type="issues",
            action="closed",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        assert issue_to_tasks(event) == []

    def test_title_in_task(self) -> None:
        payload = _issue_payload(number=123, title="My issue title")
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="octocat",
            payload=payload,
        )
        tasks = issue_to_tasks(event)
        assert "[GH#123]" in tasks[0]["title"]
        assert "My issue title" in tasks[0]["title"]


# ---------------------------------------------------------------------------
# pr_review_to_task tests
# ---------------------------------------------------------------------------


class TestPrReviewToTask:
    def test_actionable_comment_creates_task(self) -> None:
        payload = _pr_review_comment_payload(comment_body="You should fix the null check here.")
        event = WebhookEvent(
            event_type="pull_request_review_comment",
            action="created",
            repo_full_name="acme/widgets",
            sender="reviewer",
            payload=payload,
        )
        task = pr_review_to_task(event)
        assert task is not None
        assert task["priority"] == 1
        assert task["task_type"] == "fix"

    def test_non_actionable_comment_returns_none(self) -> None:
        payload = _pr_review_comment_payload(comment_body="Looks good to me! LGTM.")
        event = WebhookEvent(
            event_type="pull_request_review_comment",
            action="created",
            repo_full_name="acme/widgets",
            sender="reviewer",
            payload=payload,
        )
        assert pr_review_to_task(event) is None

    def test_role_from_test_path(self) -> None:
        payload = _pr_review_comment_payload(
            path="tests/unit/test_cache.py",
            comment_body="Please add a missing test case.",
        )
        event = WebhookEvent(
            event_type="pull_request_review_comment",
            action="created",
            repo_full_name="acme/widgets",
            sender="reviewer",
            payload=payload,
        )
        task = pr_review_to_task(event)
        assert task is not None
        assert task["role"] == "qa"

    def test_role_from_docs_path(self) -> None:
        payload = _pr_review_comment_payload(
            path="docs/setup.md",
            comment_body="Please update the installation steps.",
        )
        event = WebhookEvent(
            event_type="pull_request_review_comment",
            action="created",
            repo_full_name="acme/widgets",
            sender="reviewer",
            payload=payload,
        )
        task = pr_review_to_task(event)
        assert task is not None
        assert task["role"] == "docs"

    def test_suggestion_block_is_actionable(self) -> None:
        payload = _pr_review_comment_payload(
            comment_body="```suggestion\nreturn None\n```",
        )
        event = WebhookEvent(
            event_type="pull_request_review_comment",
            action="created",
            repo_full_name="acme/widgets",
            sender="reviewer",
            payload=payload,
        )
        assert pr_review_to_task(event) is not None


# ---------------------------------------------------------------------------
# push_to_tasks tests
# ---------------------------------------------------------------------------


class TestPushToTasks:
    def test_push_creates_qa_task(self) -> None:
        payload = _push_payload()
        event = WebhookEvent(
            event_type="push",
            action="",
            repo_full_name="acme/widgets",
            sender="pusher",
            payload=payload,
        )
        tasks = push_to_tasks(event)
        assert len(tasks) == 1
        assert tasks[0]["role"] == "qa"
        assert "Verify" in tasks[0]["title"]

    def test_push_includes_commit_messages(self) -> None:
        payload = _push_payload(
            commits=[
                {"id": "aaa111", "message": "feat: new feature"},
            ]
        )
        event = WebhookEvent(
            event_type="push",
            action="",
            repo_full_name="acme/widgets",
            sender="pusher",
            payload=payload,
        )
        tasks = push_to_tasks(event)
        assert "new feature" in tasks[0]["description"]

    def test_wrong_event_returns_empty(self) -> None:
        event = WebhookEvent(
            event_type="issues",
            action="opened",
            repo_full_name="acme/widgets",
            sender="x",
            payload={},
        )
        assert push_to_tasks(event) == []


# ---------------------------------------------------------------------------
# label_to_action tests
# ---------------------------------------------------------------------------


class TestLabelToAction:
    def test_evolve_candidate_creates_task(self) -> None:
        payload = _label_payload(label_name="evolve-candidate")
        event = WebhookEvent(
            event_type="issues",
            action="labeled",
            repo_full_name="acme/widgets",
            sender="labeler",
            payload=payload,
        )
        task = label_to_action(event)
        assert task is not None
        assert task["task_type"] == "upgrade_proposal"
        assert "[evolve]" in task["title"]

    def test_other_label_returns_none(self) -> None:
        payload = _label_payload(label_name="wontfix")
        event = WebhookEvent(
            event_type="issues",
            action="labeled",
            repo_full_name="acme/widgets",
            sender="labeler",
            payload=payload,
        )
        assert label_to_action(event) is None

    def test_wrong_action_returns_none(self) -> None:
        payload = _label_payload()
        event = WebhookEvent(
            event_type="issues",
            action="unlabeled",
            repo_full_name="acme/widgets",
            sender="labeler",
            payload=payload,
        )
        assert label_to_action(event) is None


# ---------------------------------------------------------------------------
# GitHubAppConfig tests
# ---------------------------------------------------------------------------


class TestGitHubAppConfig:
    def test_from_env_reads_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_APP_ID", "12345")
        pem = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "mysecret")
        config = GitHubAppConfig.from_env()
        assert config.app_id == "12345"
        assert "RSA" in config.private_key
        assert config.webhook_secret == "mysecret"

    def test_from_env_missing_app_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_APP_ID", raising=False)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "key")
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
        with pytest.raises(ValueError, match="GITHUB_APP_ID"):
            GitHubAppConfig.from_env()

    def test_from_env_missing_private_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
        with pytest.raises(ValueError, match="GITHUB_APP_PRIVATE_KEY"):
            GitHubAppConfig.from_env()

    def test_from_env_missing_webhook_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "key")
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        with pytest.raises(ValueError, match="GITHUB_WEBHOOK_SECRET"):
            GitHubAppConfig.from_env()

    def test_from_env_reads_pem_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pem_file = tmp_path / "key.pem"
        pem_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfilekey\n-----END RSA PRIVATE KEY-----")
        monkeypatch.setenv("GITHUB_APP_ID", "123")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", str(pem_file))
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
        config = GitHubAppConfig.from_env()
        assert "filekey" in config.private_key


# ---------------------------------------------------------------------------
# JWT building tests
# ---------------------------------------------------------------------------


class TestJwtBuilding:
    def test_base64url_encode_no_padding(self) -> None:
        result = _base64url_encode(b"test")
        assert "=" not in result

    def test_build_jwt_parts_structure(self) -> None:
        header_b64, payload_b64 = _build_jwt_parts("12345", now=1700000000.0)
        # Verify header decodes to valid JSON with RS256
        import base64

        header_raw = base64.urlsafe_b64decode(header_b64 + "==")
        header = json.loads(header_raw)
        assert header["alg"] == "RS256"
        assert header["typ"] == "JWT"
        # Verify payload has expected claims
        payload_raw = base64.urlsafe_b64decode(payload_b64 + "==")
        payload = json.loads(payload_raw)
        assert payload["iss"] == "12345"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] > payload["iat"]


# ---------------------------------------------------------------------------
# Webhook endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_valid_issue_event_returns_200(client: AsyncClient) -> None:
    payload = _issue_payload()
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
    assert data["tasks_created"] >= 1


@pytest.mark.anyio
async def test_webhook_bad_signature_returns_401(client: AsyncClient) -> None:
    payload = _issue_payload()
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": "sha256=invalid",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_missing_signature_returns_401(client: AsyncClient) -> None:
    payload = _issue_payload()
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_bad_payload_returns_400(client: AsyncClient) -> None:
    body = b"not json{{"
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
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Bad webhook payload"}


@pytest.mark.anyio
async def test_webhook_creates_tasks_in_store(client: AsyncClient) -> None:
    payload = _issue_payload(
        number=77,
        title="Store test issue",
        labels=[{"name": "bug"}],
    )
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
    task_ids = data["task_ids"]
    assert len(task_ids) >= 1
    # Verify task exists in the store via the tasks API
    task_resp = await client.get(f"/tasks/{task_ids[0]}")
    assert task_resp.status_code == 200
    task_data = task_resp.json()
    assert "[GH#77]" in task_data["title"]
    assert task_data["priority"] == 1


@pytest.mark.anyio
async def test_webhook_push_event_creates_qa_task(client: AsyncClient) -> None:
    payload = _push_payload()
    body = json.dumps(payload).encode()
    sig = _sign(body)
    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_created"] >= 1


@pytest.mark.anyio
async def test_webhook_non_actionable_comment_creates_no_tasks(client: AsyncClient) -> None:
    payload = _pr_review_comment_payload(comment_body="LGTM, looks great!")
    body = json.dumps(payload).encode()
    sig = _sign(body)
    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request_review_comment",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_created"] == 0
