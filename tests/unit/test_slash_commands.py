"""Tests for the /bernstein slash command parser."""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.github_app.slash_commands import parse_slash_command, slash_command_to_task
from bernstein.github_app.webhooks import WebhookEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    event_type: str = "issue_comment",
    action: str = "created",
    repo: str = "acme/widgets",
    sender: str = "octocat",
    payload: dict[str, Any] | None = None,
) -> WebhookEvent:
    return WebhookEvent(
        event_type=event_type,
        action=action,
        repo_full_name=repo,
        sender=sender,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# parse_slash_command
# ---------------------------------------------------------------------------


class TestParseSlashCommand:
    def test_fix_command(self) -> None:
        result = parse_slash_command("/bernstein fix the null check")
        assert result == ("fix", "the null check")

    def test_plan_command_no_args(self) -> None:
        result = parse_slash_command("/bernstein plan")
        assert result == ("plan", "")

    def test_evolve_command(self) -> None:
        result = parse_slash_command("/bernstein evolve add retry logic")
        assert result == ("evolve", "add retry logic")

    def test_qa_command(self) -> None:
        result = parse_slash_command("/bernstein qa")
        assert result == ("qa", "")

    def test_case_insensitive(self) -> None:
        result = parse_slash_command("/BERNSTEIN FIX something")
        assert result is not None
        action, args = result
        assert action == "fix"
        assert args == "something"

    def test_leading_whitespace(self) -> None:
        result = parse_slash_command("  /bernstein fix race condition")
        assert result == ("fix", "race condition")

    def test_multiline_finds_command(self) -> None:
        text = "Nice implementation!\n\n/bernstein fix the error handling\n\nThanks."
        result = parse_slash_command(text)
        assert result == ("fix", "the error handling")

    def test_no_command_returns_none(self) -> None:
        assert parse_slash_command("Just a normal comment.") is None

    def test_bernstein_without_slash_returns_none(self) -> None:
        assert parse_slash_command("bernstein fix something") is None

    def test_other_slash_command_returns_none(self) -> None:
        assert parse_slash_command("/github fix something") is None

    def test_slash_bernstein_alone_returns_none(self) -> None:
        # No action word after /bernstein
        assert parse_slash_command("/bernstein") is None


# ---------------------------------------------------------------------------
# slash_command_to_task
# ---------------------------------------------------------------------------


class TestSlashCommandToTask:
    def test_fix_action_creates_fix_task(self) -> None:
        event = _event(
            payload={
                "issue": {"number": 42, "title": "Fix the parser"},
                "comment": {"body": "/bernstein fix the null check"},
            }
        )
        task = slash_command_to_task(event, "fix", "the null check")
        assert task is not None
        assert task["task_type"] == "fix"
        assert task["priority"] == 1

    def test_plan_action_creates_planning_task(self) -> None:
        event = _event(
            payload={
                "issue": {"number": 10, "title": "Add caching"},
                "comment": {"body": "/bernstein plan"},
            }
        )
        task = slash_command_to_task(event, "plan", "")
        assert task is not None
        assert task["task_type"] == "planning"
        assert task["role"] == "manager"

    def test_evolve_action(self) -> None:
        event = _event(
            payload={
                "issue": {"number": 5, "title": "Improve error messages"},
                "comment": {"body": "/bernstein evolve"},
            }
        )
        task = slash_command_to_task(event, "evolve", "")
        assert task is not None
        assert task["task_type"] == "upgrade_proposal"

    def test_qa_action_uses_qa_role(self) -> None:
        event = _event(
            payload={
                "issue": {"number": 7, "title": "Test coverage"},
                "comment": {"body": "/bernstein qa"},
            }
        )
        task = slash_command_to_task(event, "qa", "")
        assert task is not None
        assert task["role"] == "qa"

    def test_unknown_action_returns_none(self) -> None:
        event = _event(
            payload={"issue": {"number": 1, "title": "foo"}, "comment": {"body": ""}}
        )
        assert slash_command_to_task(event, "unknown_action", "") is None

    def test_args_included_in_title(self) -> None:
        event = _event(
            payload={"issue": {"number": 3, "title": "X"}, "comment": {"body": ""}}
        )
        task = slash_command_to_task(event, "fix", "add missing null guard")
        assert task is not None
        assert "add missing null guard" in task["title"]

    def test_title_uses_issue_title_when_no_args(self) -> None:
        event = _event(
            payload={
                "issue": {"number": 99, "title": "Memory leak in spawner"},
                "comment": {"body": ""},
            }
        )
        task = slash_command_to_task(event, "fix", "")
        assert task is not None
        assert "Memory leak in spawner" in task["title"]

    def test_sender_in_description(self) -> None:
        event = _event(
            sender="reviewerbot",
            payload={"issue": {"number": 1, "title": "X"}, "comment": {"body": ""}},
        )
        task = slash_command_to_task(event, "fix", "")
        assert task is not None
        assert "reviewerbot" in task["description"]

    def test_repo_in_description(self) -> None:
        event = _event(
            repo="myorg/myrepo",
            payload={"issue": {"number": 1, "title": "X"}, "comment": {"body": ""}},
        )
        task = slash_command_to_task(event, "plan", "")
        assert task is not None
        assert "myorg/myrepo" in task["description"]
