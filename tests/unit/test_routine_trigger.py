"""Tests for Claude Code Routine trigger source."""

from __future__ import annotations

from bernstein.core.trigger_sources.routine import (
    extract_github_context,
    normalize_routine_webhook,
)


class TestNormalizeRoutineWebhook:
    def test_basic_goal_payload(self) -> None:
        payload = {"goal": "Review PR #42", "session_id": "sess_123"}
        event = normalize_routine_webhook({}, payload)
        assert event.source == "routine"
        assert event.message == "Review PR #42"
        assert event.metadata["source_type"] == "routine"
        assert event.metadata["routine_session_id"] == "sess_123"

    def test_github_context_extraction(self) -> None:
        payload = {
            "goal": "Review this PR",
            "github": {
                "event_type": "pull_request.opened",
                "repo": "chernistry/bernstein",
                "pr_number": 42,
                "ref": "refs/heads/feature-x",
                "author": "contributor",
            },
        }
        event = normalize_routine_webhook({}, payload)
        assert event.metadata["github_event"] == "pull_request.opened"
        assert event.metadata["github_pr_number"] == 42
        assert event.metadata["github_repo"] == "chernistry/bernstein"

    def test_scenario_id_in_metadata(self) -> None:
        payload = {"scenario_id": "pr-review-comprehensive", "goal": "Run review"}
        event = normalize_routine_webhook({}, payload)
        assert event.metadata["scenario_id"] == "pr-review-comprehensive"

    def test_empty_payload(self) -> None:
        event = normalize_routine_webhook({}, {})
        assert event.source == "routine"
        assert event.message == ""

    def test_message_truncation(self) -> None:
        payload = {"goal": "x" * 1000}
        event = normalize_routine_webhook({}, payload)
        assert len(event.message) == 500

    def test_text_fallback(self) -> None:
        payload = {"text": "Sentry alert fired"}
        event = normalize_routine_webhook({}, payload)
        assert event.message == "Sentry alert fired"


class TestExtractGithubContext:
    def test_full_context(self) -> None:
        payload = {
            "github": {
                "event_type": "pull_request.opened",
                "repo": "org/repo",
                "pr_number": 123,
                "pr_title": "Add feature",
                "author": "dev",
                "labels": ["enhancement"],
                "changed_files": ["src/foo.py"],
            }
        }
        ctx = extract_github_context(payload)
        assert ctx["pr_number"] == 123
        assert ctx["labels"] == ["enhancement"]
        assert ctx["changed_files"] == ["src/foo.py"]

    def test_empty_github(self) -> None:
        ctx = extract_github_context({})
        assert ctx["event_type"] == ""
        assert ctx["pr_number"] is None
        assert ctx["labels"] == []

    def test_partial_context(self) -> None:
        payload = {"github": {"repo": "org/repo"}}
        ctx = extract_github_context(payload)
        assert ctx["repo"] == "org/repo"
        assert ctx["author"] == ""
