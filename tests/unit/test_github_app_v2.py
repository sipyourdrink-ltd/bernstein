"""Tests for GitHub App v2 additions: trigger labels, handler classes."""

from __future__ import annotations

from typing import Any

from bernstein.github_app.mapper import (
    IssueHandler,
    PRCommentHandler,
    PushHandler,
    SlashCommandHandler,
    trigger_label_to_task,
)
from bernstein.github_app.webhooks import WebhookEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    event_type: str,
    action: str,
    payload: dict[str, Any],
    repo: str = "acme/widgets",
    sender: str = "octocat",
) -> WebhookEvent:
    return WebhookEvent(
        event_type=event_type,
        action=action,
        repo_full_name=repo,
        sender=sender,
        payload=payload,
    )


def _label_event(
    label_name: str,
    issue_number: int = 42,
    issue_title: str = "Test issue",
    body: str = "Some body text",
    action: str = "labeled",
) -> WebhookEvent:
    return _event(
        event_type="issues",
        action=action,
        payload={
            "action": action,
            "label": {"name": label_name},
            "issue": {
                "number": issue_number,
                "title": issue_title,
                "body": body,
                "labels": [{"name": label_name}],
            },
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "octocat"},
        },
    )


# ---------------------------------------------------------------------------
# trigger_label_to_task
# ---------------------------------------------------------------------------


class TestTriggerLabelToTask:
    def test_bernstein_label_creates_task(self) -> None:
        event = _label_event("bernstein")
        task = trigger_label_to_task(event)
        assert task is not None
        assert "[GH#42]" in task["title"]

    def test_agent_fix_label_creates_fix_task(self) -> None:
        event = _label_event("agent-fix")
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["task_type"] == "fix"
        assert task["priority"] == 1  # agent-fix is priority 1

    def test_agent_task_label_creates_standard_task(self) -> None:
        event = _label_event("agent-task")
        task = trigger_label_to_task(event)
        assert task is not None
        assert task["task_type"] == "standard"

    def test_bug_label_not_a_trigger(self) -> None:
        event = _label_event("bug")
        assert trigger_label_to_task(event) is None

    def test_unlabeled_action_returns_none(self) -> None:
        event = _label_event("bernstein", action="unlabeled")
        assert trigger_label_to_task(event) is None

    def test_sender_in_description(self) -> None:
        event = _label_event("bernstein")
        task = trigger_label_to_task(event)
        assert task is not None
        assert "octocat" in task["description"]

    def test_repo_in_description(self) -> None:
        event = _label_event("agent-fix")
        task = trigger_label_to_task(event)
        assert task is not None
        assert "acme/widgets" in task["description"]

    def test_issue_body_in_description(self) -> None:
        event = _label_event("bernstein", body="This is the issue body text.")
        task = trigger_label_to_task(event)
        assert task is not None
        assert "This is the issue body text." in task["description"]


# ---------------------------------------------------------------------------
# IssueHandler
# ---------------------------------------------------------------------------


class TestIssueHandler:
    def test_handle_opened(self) -> None:
        event = _event(
            event_type="issues",
            action="opened",
            payload={
                "action": "opened",
                "issue": {"number": 1, "title": "Bug report", "body": "", "labels": []},
                "repository": {"full_name": "acme/widgets"},
                "sender": {"login": "user"},
            },
        )
        tasks = IssueHandler().handle(event)
        assert len(tasks) == 1

    def test_handle_labeled_bernstein(self) -> None:
        event = _label_event("bernstein")
        tasks = IssueHandler().handle(event)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "standard"

    def test_handle_labeled_agent_fix(self) -> None:
        event = _label_event("agent-fix")
        tasks = IssueHandler().handle(event)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "fix"

    def test_handle_labeled_evolve_candidate(self) -> None:
        event = _label_event("evolve-candidate")
        tasks = IssueHandler().handle(event)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "upgrade_proposal"

    def test_handle_labeled_unknown_returns_empty(self) -> None:
        event = _label_event("wontfix")
        tasks = IssueHandler().handle(event)
        assert tasks == []

    def test_handle_closed_returns_empty(self) -> None:
        event = _event(
            event_type="issues",
            action="closed",
            payload={"action": "closed"},
        )
        tasks = IssueHandler().handle(event)
        assert tasks == []


# ---------------------------------------------------------------------------
# PRCommentHandler
# ---------------------------------------------------------------------------


class TestPRCommentHandler:
    def test_slash_command_takes_precedence(self) -> None:
        event = _event(
            event_type="issue_comment",
            action="created",
            payload={
                "comment": {"body": "/bernstein fix the race condition"},
                "issue": {"number": 5, "title": "Bug"},
                "repository": {"full_name": "acme/widgets"},
                "sender": {"login": "reviewer"},
            },
        )
        tasks = PRCommentHandler().handle(event)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "fix"

    def test_actionable_review_comment_without_slash(self) -> None:
        event = _event(
            event_type="pull_request_review_comment",
            action="created",
            payload={
                "comment": {"body": "You should fix the null check here.", "path": "src/foo.py"},
                "pull_request": {"number": 10, "title": "Add feature"},
                "repository": {"full_name": "acme/widgets"},
                "sender": {"login": "reviewer"},
            },
        )
        tasks = PRCommentHandler().handle(event)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "fix"

    def test_non_actionable_returns_empty(self) -> None:
        event = _event(
            event_type="pull_request_review_comment",
            action="created",
            payload={
                "comment": {"body": "LGTM! 👍", "path": ""},
                "pull_request": {"number": 1, "title": "X"},
                "repository": {"full_name": "acme/widgets"},
                "sender": {"login": "reviewer"},
            },
        )
        tasks = PRCommentHandler().handle(event)
        assert tasks == []

    def test_unknown_slash_command_falls_through_to_review(self) -> None:
        """Unknown /bernstein action → no slash task; check review fallback."""
        event = _event(
            event_type="issue_comment",
            action="created",
            payload={
                "comment": {"body": "/bernstein unknownaction"},
                "issue": {"number": 1, "title": "X"},
                "repository": {"full_name": "acme/widgets"},
                "sender": {"login": "x"},
            },
        )
        # /bernstein unknownaction is recognised as a slash command attempt
        # but returns None from slash_command_to_task, and the text doesn't
        # contain actionable review language — so result is empty
        tasks = PRCommentHandler().handle(event)
        assert tasks == []


# ---------------------------------------------------------------------------
# PushHandler
# ---------------------------------------------------------------------------


class TestPushHandler:
    def test_push_creates_qa_task(self) -> None:
        event = _event(
            event_type="push",
            action="",
            payload={
                "ref": "refs/heads/main",
                "commits": [{"id": "aaa111", "message": "feat: new thing"}],
                "repository": {"full_name": "acme/widgets"},
                "sender": {"login": "pusher"},
            },
        )
        tasks = PushHandler().handle(event)
        assert len(tasks) == 1
        assert tasks[0]["role"] == "qa"

    def test_non_push_event_returns_empty(self) -> None:
        event = _event(event_type="issues", action="opened", payload={})
        assert PushHandler().handle(event) == []


# ---------------------------------------------------------------------------
# SlashCommandHandler
# ---------------------------------------------------------------------------


class TestSlashCommandHandler:
    def test_parses_and_returns_task(self) -> None:
        event = _event(
            event_type="issue_comment",
            action="created",
            payload={
                "issue": {"number": 7, "title": "Flaky tests"},
                "comment": {"body": "/bernstein qa"},
            },
        )
        task = SlashCommandHandler().handle(event, "/bernstein qa run the suite")
        assert task is not None
        assert task["role"] == "qa"

    def test_no_command_returns_none(self) -> None:
        event = _event(
            event_type="issue_comment",
            action="created",
            payload={"issue": {"number": 1, "title": "X"}, "comment": {"body": ""}},
        )
        assert SlashCommandHandler().handle(event, "just a normal comment") is None

    def test_plan_command(self) -> None:
        event = _event(
            event_type="issue_comment",
            action="created",
            payload={"issue": {"number": 3, "title": "Add feature"}, "comment": {"body": ""}},
        )
        task = SlashCommandHandler().handle(event, "/bernstein plan decompose the feature")
        assert task is not None
        assert task["task_type"] == "planning"
