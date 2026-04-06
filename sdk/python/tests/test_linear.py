"""Tests for bernstein_sdk.adapters.linear."""

from __future__ import annotations

import pytest

from bernstein_sdk.adapters.linear import (
    LinearAdapter,
    LinearIssueRef,
    _estimate_to_scope,
    _labels_to_complexity,
)
from bernstein_sdk.models import TaskComplexity, TaskScope, TaskStatus


class TestLinearIssueRef:
    def test_from_webhook_payload_valid(self) -> None:
        payload = {
            "action": "create",
            "type": "Issue",
            "data": {
                "identifier": "ENG-5",
                "title": "Fix crash",
                "description": "App crashes on startup",
                "priority": 1,
                "estimate": 3.0,
                "state": {"name": "In Progress", "type": "started"},
                "assignee": {"email": "dev@example.com"},
                "team": {"id": "team-uuid"},
                "labels": {"nodes": [{"name": "backend"}]},
            },
        }
        ref = LinearIssueRef.from_webhook_payload(payload)
        assert ref is not None
        assert ref.identifier == "ENG-5"
        assert ref.title == "Fix crash"
        assert ref.state_name == "In Progress"
        assert ref.state_type == "started"
        assert ref.priority == 1
        assert ref.estimate == pytest.approx(3.0)
        assert ref.labels == ["backend"]
        assert ref.assignee_email == "dev@example.com"
        assert ref.team_id == "team-uuid"

    def test_from_webhook_payload_wrong_type(self) -> None:
        payload = {"action": "create", "type": "Comment", "data": {}}
        ref = LinearIssueRef.from_webhook_payload(payload)
        assert ref is None

    def test_from_webhook_payload_missing_data(self) -> None:
        payload = {"action": "create", "type": "Issue"}
        ref = LinearIssueRef.from_webhook_payload(payload)
        assert ref is None

    def test_from_webhook_payload_minimal(self) -> None:
        payload = {
            "action": "update",
            "type": "Issue",
            "data": {"identifier": "ENG-1", "title": "T"},
        }
        ref = LinearIssueRef.from_webhook_payload(payload)
        assert ref is not None
        assert ref.state_name == "Todo"
        assert ref.state_type == "unstarted"
        assert ref.priority == 0
        assert ref.labels == []
        assert ref.assignee_email is None

    def test_from_graphql_response(self) -> None:
        data = {
            "identifier": "ENG-99",
            "title": "GraphQL issue",
            "description": "desc",
            "priority": 2,
            "estimate": 5.0,
            "state": {"name": "Done", "type": "completed"},
            "assignee": None,
            "team": {"id": "t1"},
            "labels": {"nodes": []},
        }
        ref = LinearIssueRef.from_graphql_response(data)
        assert ref.identifier == "ENG-99"
        assert ref.state_type == "completed"


class TestLinearAdapter:
    def _make_adapter(self) -> LinearAdapter:
        return LinearAdapter(api_key="lin_api_test123")

    def _make_issue(
        self,
        identifier: str = "ENG-10",
        title: str = "Test issue",
        state_name: str = "Todo",
        state_type: str = "unstarted",
        priority: int = 2,
        estimate: float | None = None,
        labels: list[str] | None = None,
        team_id: str = "team-1",
    ) -> LinearIssueRef:
        return LinearIssueRef(
            identifier=identifier,
            title=title,
            description="",
            state_name=state_name,
            state_type=state_type,
            priority=priority,
            estimate=estimate,
            labels=labels or [],
            team_id=team_id,
            assignee_email=None,
        )

    def test_task_from_issue_defaults(self) -> None:
        adapter = self._make_adapter()
        issue = self._make_issue()
        task = adapter.task_from_issue(issue)
        assert task.title == "[ENG-10] Test issue"
        assert task.external_ref == "linear:ENG-10"
        assert task.role == "backend"
        assert task.metadata["linear_identifier"] == "ENG-10"
        assert task.metadata["linear_state"] == "Todo"

    def test_task_from_issue_priority_mapping(self) -> None:
        adapter = self._make_adapter()
        for linear_prio, expected in [(1, 1), (2, 1), (3, 2), (4, 3), (0, 2)]:
            issue = self._make_issue(priority=linear_prio)
            task = adapter.task_from_issue(issue)
            assert task.priority == expected

    def test_task_from_issue_scope_from_estimate(self) -> None:
        adapter = self._make_adapter()
        for estimate, expected in [
            (1.0, TaskScope.SMALL),
            (3.0, TaskScope.MEDIUM),
            (8.0, TaskScope.LARGE),
        ]:
            issue = self._make_issue(estimate=estimate)
            task = adapter.task_from_issue(issue)
            assert task.scope == expected

    def test_task_from_issue_team_role_mapping(self) -> None:
        adapter = LinearAdapter(
            api_key="lin_api_test",
            team_id_to_role={"team-frontend": "frontend"},
        )
        issue = self._make_issue(team_id="team-frontend")
        task = adapter.task_from_issue(issue)
        assert task.role == "frontend"

    def test_task_from_webhook_valid_create(self) -> None:
        adapter = self._make_adapter()
        payload = {
            "action": "create",
            "type": "Issue",
            "data": {
                "identifier": "ENG-3",
                "title": "New feature",
                "description": "",
                "priority": 2,
                "state": {"name": "Todo", "type": "unstarted"},
                "team": {"id": "t1"},
                "labels": {"nodes": []},
            },
        }
        task = adapter.task_from_webhook(payload)
        assert task is not None
        assert task.title == "[ENG-3] New feature"

    def test_task_from_webhook_skips_remove(self) -> None:
        adapter = self._make_adapter()
        payload = {"action": "remove", "type": "Issue", "data": {}}
        assert adapter.task_from_webhook(payload) is None

    def test_task_from_webhook_skips_completed(self) -> None:
        adapter = self._make_adapter()
        payload = {
            "action": "update",
            "type": "Issue",
            "data": {
                "identifier": "ENG-4",
                "title": "Done issue",
                "priority": 2,
                "state": {"name": "Done", "type": "completed"},
                "team": {"id": "t1"},
                "labels": {"nodes": []},
            },
        }
        assert adapter.task_from_webhook(payload) is None

    def test_task_from_webhook_skips_cancelled(self) -> None:
        adapter = self._make_adapter()
        payload = {
            "action": "update",
            "type": "Issue",
            "data": {
                "identifier": "ENG-5",
                "title": "Cancelled",
                "priority": 2,
                "state": {"name": "Cancelled", "type": "cancelled"},
                "team": {"id": "t1"},
                "labels": {"nodes": []},
            },
        }
        assert adapter.task_from_webhook(payload) is None

    def test_from_env_missing_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="LINEAR_API_KEY"):
            LinearAdapter.from_env()

    def test_from_env_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        adapter = LinearAdapter.from_env()
        assert adapter._api_key == "lin_api_test"
        adapter.close()

    def test_context_manager(self) -> None:
        with LinearAdapter(api_key="lin_api_test") as adapter:
            assert adapter._api_key == "lin_api_test"

    def test_sync_task_to_linear_no_ref(self) -> None:
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
        assert adapter.sync_task_to_linear(task) is False
        adapter.close()

    def test_sync_task_to_linear_jira_ref(self) -> None:
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
            external_ref="jira:PROJ-1",
        )
        assert adapter.sync_task_to_linear(task) is False
        adapter.close()


class TestLinearHelpers:
    def test_estimate_to_scope(self) -> None:
        assert _estimate_to_scope(None) == TaskScope.MEDIUM
        assert _estimate_to_scope(1.0) == TaskScope.SMALL
        assert _estimate_to_scope(2.0) == TaskScope.SMALL
        assert _estimate_to_scope(3.0) == TaskScope.MEDIUM
        assert _estimate_to_scope(5.0) == TaskScope.MEDIUM
        assert _estimate_to_scope(6.0) == TaskScope.LARGE

    def test_labels_to_complexity(self) -> None:
        assert _labels_to_complexity([]) == TaskComplexity.MEDIUM
        assert _labels_to_complexity(["architecture"]) == TaskComplexity.HIGH
        assert _labels_to_complexity(["performance"]) == TaskComplexity.HIGH
        assert _labels_to_complexity(["chore"]) == TaskComplexity.LOW
        assert _labels_to_complexity(["documentation"]) == TaskComplexity.LOW
        assert _labels_to_complexity(["normal"]) == TaskComplexity.MEDIUM
