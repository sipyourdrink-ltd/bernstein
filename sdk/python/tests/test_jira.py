"""Tests for bernstein_sdk.adapters.jira."""

from __future__ import annotations

import pytest

from bernstein_sdk.adapters.jira import (
    JiraAdapter,
    JiraIssueRef,
    _extract_adf_text,
    _labels_to_complexity,
    _story_points_to_scope,
)
from bernstein_sdk.models import TaskComplexity, TaskScope, TaskStatus


class TestJiraIssueRef:
    def test_from_api_response_minimal(self) -> None:
        data = {
            "key": "PROJ-1",
            "fields": {
                "summary": "Fix crash",
                "status": {"name": "In Progress"},
                "priority": {"name": "High"},
            },
        }
        ref = JiraIssueRef.from_api_response(data)
        assert ref.key == "PROJ-1"
        assert ref.summary == "Fix crash"
        assert ref.status == "In Progress"
        assert ref.priority == "high"
        assert ref.story_points is None
        assert ref.labels == []
        assert ref.assignee_email is None

    def test_from_api_response_full(self) -> None:
        data = {
            "key": "PROJ-42",
            "fields": {
                "summary": "Add rate limiting",
                "description": {
                    "type": "doc",
                    "content": [{"type": "text", "text": "desc text"}],
                },
                "status": {"name": "To Do"},
                "priority": {"name": "Medium"},
                "story_points": 5.0,
                "labels": ["backend", "security"],
                "assignee": {"emailAddress": "dev@example.com"},
            },
        }
        ref = JiraIssueRef.from_api_response(data)
        assert ref.key == "PROJ-42"
        assert ref.story_points == pytest.approx(5.0)
        assert ref.labels == ["backend", "security"]
        assert ref.assignee_email == "dev@example.com"
        assert "desc text" in ref.description

    def test_from_api_response_adf_description(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "paragraph text"}],
                }
            ],
        }
        data = {
            "key": "X-1",
            "fields": {
                "summary": "S",
                "description": adf,
                "status": {"name": "Open"},
                "priority": {"name": "Low"},
            },
        }
        ref = JiraIssueRef.from_api_response(data)
        assert "paragraph text" in ref.description

    def test_from_webhook_payload_valid(self) -> None:
        payload = {
            "issue": {
                "key": "PROJ-5",
                "fields": {
                    "summary": "Bug report",
                    "status": {"name": "To Do"},
                    "priority": {"name": "High"},
                },
            }
        }
        ref = JiraIssueRef.from_webhook_payload(payload)
        assert ref is not None
        assert ref.key == "PROJ-5"

    def test_from_webhook_payload_missing_issue(self) -> None:
        ref = JiraIssueRef.from_webhook_payload({"webhookEvent": "something"})
        assert ref is None


class TestJiraAdapter:
    def _make_adapter(self) -> JiraAdapter:
        return JiraAdapter(
            base_url="https://example.atlassian.net",
            email="test@example.com",
            api_token="token123",
        )

    def _make_issue(
        self,
        key: str = "PROJ-10",
        summary: str = "Test issue",
        status: str = "To Do",
        priority: str = "medium",
        story_points: float | None = None,
        labels: list[str] | None = None,
    ) -> JiraIssueRef:
        return JiraIssueRef(
            key=key,
            summary=summary,
            description="",
            status=status,
            priority=priority,
            story_points=story_points,
            labels=labels or [],
            assignee_email=None,
        )

    def test_task_from_issue_defaults(self) -> None:
        adapter = self._make_adapter()
        issue = self._make_issue()
        task = adapter.task_from_issue(issue)
        assert task.title == "[PROJ-10] Test issue"
        assert task.external_ref == "jira:PROJ-10"
        assert task.role == "backend"
        assert task.metadata["jira_key"] == "PROJ-10"

    def test_task_from_issue_priority_mapping(self) -> None:
        adapter = self._make_adapter()
        for priority, expected in [("high", 1), ("medium", 2), ("low", 3)]:
            issue = self._make_issue(priority=priority)
            task = adapter.task_from_issue(issue)
            assert task.priority == expected

    def test_task_from_issue_scope_from_story_points(self) -> None:
        adapter = self._make_adapter()
        for points, expected_scope in [
            (2.0, TaskScope.SMALL),
            (5.0, TaskScope.MEDIUM),
            (13.0, TaskScope.LARGE),
        ]:
            issue = self._make_issue(story_points=points)
            task = adapter.task_from_issue(issue)
            assert task.scope == expected_scope

    def test_task_from_issue_project_key_role(self) -> None:
        adapter = JiraAdapter(
            base_url="https://example.atlassian.net",
            email="test@example.com",
            api_token="token",
            project_key_to_role={"FRONT": "frontend"},
        )
        issue = self._make_issue(key="FRONT-5")
        task = adapter.task_from_issue(issue)
        assert task.role == "frontend"

    def test_task_from_webhook_valid(self) -> None:
        adapter = self._make_adapter()
        payload = {
            "issue": {
                "key": "PROJ-3",
                "fields": {
                    "summary": "New bug",
                    "status": {"name": "In Progress"},
                    "priority": {"name": "High"},
                },
            }
        }
        task = adapter.task_from_webhook(payload)
        assert task is not None
        assert task.title == "[PROJ-3] New bug"

    def test_task_from_webhook_skips_done_issues(self) -> None:
        adapter = self._make_adapter()
        payload = {
            "issue": {
                "key": "PROJ-4",
                "fields": {
                    "summary": "Already done",
                    "status": {"name": "Done"},
                    "priority": {"name": "Low"},
                },
            }
        }
        task = adapter.task_from_webhook(payload)
        assert task is None

    def test_task_from_webhook_skips_cancelled_issues(self) -> None:
        adapter = self._make_adapter()
        payload = {
            "issue": {
                "key": "PROJ-5",
                "fields": {
                    "summary": "Cancelled",
                    "status": {"name": "Cancelled"},
                    "priority": {"name": "Low"},
                },
            }
        }
        assert adapter.task_from_webhook(payload) is None

    def test_task_from_webhook_empty_payload(self) -> None:
        adapter = self._make_adapter()
        assert adapter.task_from_webhook({}) is None

    def test_from_env_missing_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_EMAIL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="Missing environment variables"):
            JiraAdapter.from_env()

    def test_from_env_all_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "tok")
        adapter = JiraAdapter.from_env()
        assert adapter._base_url == "https://test.atlassian.net"

    def test_sync_task_to_jira_no_ref(self) -> None:
        from bernstein_sdk.models import TaskResponse

        adapter = self._make_adapter()
        task = TaskResponse(
            id="t1",
            title="T",
            role="backend",
            status=TaskStatus.DONE,
            priority=1,
            scope="small",
            complexity="medium",
            external_ref="",
        )
        assert adapter.sync_task_to_jira(task) is False

    def test_sync_task_to_jira_non_jira_ref(self) -> None:
        from bernstein_sdk.models import TaskResponse

        adapter = self._make_adapter()
        task = TaskResponse(
            id="t2",
            title="T",
            role="backend",
            status=TaskStatus.DONE,
            priority=1,
            scope="small",
            complexity="medium",
            external_ref="linear:ENG-1",
        )
        assert adapter.sync_task_to_jira(task) is False


class TestHelpers:
    def test_story_points_to_scope(self) -> None:
        assert _story_points_to_scope(None) == TaskScope.MEDIUM
        assert _story_points_to_scope(1.0) == TaskScope.SMALL
        assert _story_points_to_scope(3.0) == TaskScope.SMALL
        assert _story_points_to_scope(4.0) == TaskScope.MEDIUM
        assert _story_points_to_scope(8.0) == TaskScope.MEDIUM
        assert _story_points_to_scope(9.0) == TaskScope.LARGE

    def test_labels_to_complexity(self) -> None:
        assert _labels_to_complexity([]) == TaskComplexity.MEDIUM
        assert _labels_to_complexity(["security"]) == TaskComplexity.HIGH
        assert _labels_to_complexity(["complex"]) == TaskComplexity.HIGH
        assert _labels_to_complexity(["docs"]) == TaskComplexity.LOW
        assert _labels_to_complexity(["simple"]) == TaskComplexity.LOW
        assert _labels_to_complexity(["random"]) == TaskComplexity.MEDIUM

    def test_extract_adf_text_empty(self) -> None:
        assert _extract_adf_text({}) == ""

    def test_extract_adf_text_nested(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "hello"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "world"}],
                },
            ],
        }
        result = _extract_adf_text(adf)
        assert "hello" in result
        assert "world" in result
