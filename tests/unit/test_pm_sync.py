"""Tests for Linear/Asana/Shortcut project management sync."""

from __future__ import annotations

import pytest

from bernstein.core.protocols.pm_sync import (
    PMClient,
    PMProvider,
    PMSyncConfig,
    PMSyncResult,
    PMTask,
    PMTaskStatus,
    convert_bernstein_status,
    render_sync_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def linear_config() -> PMSyncConfig:
    return PMSyncConfig(
        provider=PMProvider.LINEAR,
        api_key_env="LINEAR_API_KEY",
        project_id="proj-lin-1",
    )


@pytest.fixture()
def asana_config() -> PMSyncConfig:
    return PMSyncConfig(
        provider=PMProvider.ASANA,
        api_key_env="ASANA_TOKEN",
        project_id="12345",
        workspace_id="ws-99",
    )


@pytest.fixture()
def shortcut_config() -> PMSyncConfig:
    return PMSyncConfig(
        provider=PMProvider.SHORTCUT,
        api_key_env="SHORTCUT_TOKEN",
        project_id="42",
    )


@pytest.fixture()
def sample_task() -> PMTask:
    return PMTask(
        provider_id="t-1",
        title="Fix login bug",
        description="Users cannot log in with SSO",
        status=PMTaskStatus.TODO,
        assignee="alice",
        priority="high",
        labels=("bug", "auth"),
        url="https://example.com/t-1",
    )


@pytest.fixture()
def client() -> PMClient:
    return PMClient()


# ---------------------------------------------------------------------------
# Tests — PMProvider enum
# ---------------------------------------------------------------------------


class TestPMProvider:
    def test_enum_values(self) -> None:
        assert PMProvider.LINEAR == "linear"
        assert PMProvider.ASANA == "asana"
        assert PMProvider.SHORTCUT == "shortcut"

    def test_enum_is_str(self) -> None:
        assert isinstance(PMProvider.LINEAR, str)


# ---------------------------------------------------------------------------
# Tests — PMTask dataclass
# ---------------------------------------------------------------------------


class TestPMTask:
    def test_frozen(self, sample_task: PMTask) -> None:
        with pytest.raises(AttributeError):
            sample_task.title = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        task = PMTask(
            provider_id="x",
            title="t",
            description="d",
            status=PMTaskStatus.TODO,
        )
        assert task.assignee is None
        assert task.priority is None
        assert task.labels == ()
        assert task.url is None

    def test_labels_are_tuple(self, sample_task: PMTask) -> None:
        assert isinstance(sample_task.labels, tuple)


# ---------------------------------------------------------------------------
# Tests — PMSyncConfig dataclass
# ---------------------------------------------------------------------------


class TestPMSyncConfig:
    def test_frozen(self, linear_config: PMSyncConfig) -> None:
        with pytest.raises(AttributeError):
            linear_config.project_id = "other"  # type: ignore[misc]

    def test_workspace_default_none(self, linear_config: PMSyncConfig) -> None:
        assert linear_config.workspace_id is None

    def test_workspace_set(self, asana_config: PMSyncConfig) -> None:
        assert asana_config.workspace_id == "ws-99"


# ---------------------------------------------------------------------------
# Tests — PMSyncResult dataclass
# ---------------------------------------------------------------------------


class TestPMSyncResult:
    def test_frozen(self) -> None:
        result = PMSyncResult(created=1, updated=2, skipped=3)
        with pytest.raises(AttributeError):
            result.created = 9  # type: ignore[misc]

    def test_errors_default_empty(self) -> None:
        result = PMSyncResult(created=0, updated=0, skipped=0)
        assert result.errors == ()

    def test_errors_tuple(self) -> None:
        result = PMSyncResult(created=0, updated=0, skipped=0, errors=("oops",))
        assert result.errors == ("oops",)


# ---------------------------------------------------------------------------
# Tests — get_headers
# ---------------------------------------------------------------------------


class TestGetHeaders:
    def test_linear_headers(self, linear_config: PMSyncConfig) -> None:
        headers = PMClient.get_headers(linear_config)
        assert headers["Authorization"] == "${LINEAR_API_KEY}"
        assert headers["Content-Type"] == "application/json"

    def test_asana_headers(self, asana_config: PMSyncConfig) -> None:
        headers = PMClient.get_headers(asana_config)
        assert headers["Authorization"] == "Bearer ${ASANA_TOKEN}"

    def test_shortcut_headers(self, shortcut_config: PMSyncConfig) -> None:
        headers = PMClient.get_headers(shortcut_config)
        assert "Shortcut-Token" in headers
        assert headers["Shortcut-Token"] == "${SHORTCUT_TOKEN}"


# ---------------------------------------------------------------------------
# Tests — get_api_url
# ---------------------------------------------------------------------------


class TestGetApiUrl:
    def test_linear_url(self) -> None:
        url = PMClient.get_api_url(PMProvider.LINEAR, "/graphql")
        assert url == "https://api.linear.app/graphql"

    def test_asana_url(self) -> None:
        url = PMClient.get_api_url(PMProvider.ASANA, "/tasks")
        assert url == "https://app.asana.com/api/1.0/tasks"

    def test_shortcut_url(self) -> None:
        url = PMClient.get_api_url(PMProvider.SHORTCUT, "/stories")
        assert url == "https://api.app.shortcut.com/api/v3/stories"

    def test_no_double_slash(self) -> None:
        url = PMClient.get_api_url(PMProvider.LINEAR, "graphql")
        assert "//" not in url.split("://")[1]


# ---------------------------------------------------------------------------
# Tests — build_list_tasks_request
# ---------------------------------------------------------------------------


class TestBuildListTasksRequest:
    def test_linear_list(self, client: PMClient, linear_config: PMSyncConfig) -> None:
        req = client.build_list_tasks_request(linear_config)
        assert req["method"] == "POST"
        assert "graphql" in req["url"]
        assert "query" in req["body"]
        assert "proj-lin-1" in req["body"]["query"]

    def test_asana_list(self, client: PMClient, asana_config: PMSyncConfig) -> None:
        req = client.build_list_tasks_request(asana_config)
        assert req["method"] == "GET"
        assert "/projects/12345/tasks" in req["url"]
        assert req["body"] is None

    def test_shortcut_list(self, client: PMClient, shortcut_config: PMSyncConfig) -> None:
        req = client.build_list_tasks_request(shortcut_config)
        assert req["method"] == "GET"
        assert "/projects/42/stories" in req["url"]


# ---------------------------------------------------------------------------
# Tests — build_create_task_request
# ---------------------------------------------------------------------------


class TestBuildCreateTaskRequest:
    def test_linear_create(self, client: PMClient, linear_config: PMSyncConfig, sample_task: PMTask) -> None:
        req = client.build_create_task_request(linear_config, sample_task)
        assert req["method"] == "POST"
        assert "mutation" in req["body"]["query"]
        assert "Fix login bug" in req["body"]["query"]

    def test_asana_create(self, client: PMClient, asana_config: PMSyncConfig, sample_task: PMTask) -> None:
        req = client.build_create_task_request(asana_config, sample_task)
        assert req["method"] == "POST"
        assert req["url"].endswith("/tasks")
        assert req["body"]["data"]["name"] == "Fix login bug"
        assert "12345" in req["body"]["data"]["projects"]

    def test_asana_create_with_assignee(
        self, client: PMClient, asana_config: PMSyncConfig, sample_task: PMTask
    ) -> None:
        req = client.build_create_task_request(asana_config, sample_task)
        assert req["body"]["data"]["assignee"] == "alice"

    def test_asana_create_no_assignee(self, client: PMClient, asana_config: PMSyncConfig) -> None:
        task = PMTask(provider_id="x", title="t", description="d", status=PMTaskStatus.TODO)
        req = client.build_create_task_request(asana_config, task)
        assert "assignee" not in req["body"]["data"]

    def test_shortcut_create(self, client: PMClient, shortcut_config: PMSyncConfig, sample_task: PMTask) -> None:
        req = client.build_create_task_request(shortcut_config, sample_task)
        assert req["method"] == "POST"
        assert req["body"]["name"] == "Fix login bug"
        assert req["body"]["project_id"] == "42"

    def test_shortcut_create_labels(self, client: PMClient, shortcut_config: PMSyncConfig, sample_task: PMTask) -> None:
        req = client.build_create_task_request(shortcut_config, sample_task)
        assert len(req["body"]["labels"]) == 2
        assert req["body"]["labels"][0]["name"] == "bug"

    def test_shortcut_create_no_labels(self, client: PMClient, shortcut_config: PMSyncConfig) -> None:
        task = PMTask(provider_id="x", title="t", description="d", status=PMTaskStatus.TODO)
        req = client.build_create_task_request(shortcut_config, task)
        assert "labels" not in req["body"]


# ---------------------------------------------------------------------------
# Tests — build_update_status_request
# ---------------------------------------------------------------------------


class TestBuildUpdateStatusRequest:
    def test_linear_update(self, client: PMClient, linear_config: PMSyncConfig) -> None:
        req = client.build_update_status_request(linear_config, "issue-1", PMTaskStatus.DONE)
        assert req["method"] == "POST"
        assert "issueUpdate" in req["body"]["query"]
        assert "Done" in req["body"]["query"]

    def test_asana_update_done(self, client: PMClient, asana_config: PMSyncConfig) -> None:
        req = client.build_update_status_request(asana_config, "task-99", PMTaskStatus.DONE)
        assert req["method"] == "PUT"
        assert req["body"]["data"]["completed"] is True

    def test_asana_update_todo(self, client: PMClient, asana_config: PMSyncConfig) -> None:
        req = client.build_update_status_request(asana_config, "task-99", PMTaskStatus.TODO)
        assert req["body"]["data"]["completed"] is False

    def test_shortcut_update(self, client: PMClient, shortcut_config: PMSyncConfig) -> None:
        req = client.build_update_status_request(shortcut_config, "story-7", PMTaskStatus.IN_PROGRESS)
        assert req["method"] == "PUT"
        assert "/stories/story-7" in req["url"]
        assert req["body"]["workflow_state_id"] == "started"


# ---------------------------------------------------------------------------
# Tests — parse_linear_task
# ---------------------------------------------------------------------------


class TestParseLinearTask:
    def test_full_parse(self) -> None:
        data = {
            "id": "LIN-42",
            "title": "Add caching",
            "description": "Implement Redis caching",
            "state": {"name": "In Progress"},
            "assignee": {"name": "Bob"},
            "priorityLabel": "Urgent",
            "labels": {"nodes": [{"name": "perf"}, {"name": "backend"}]},
            "url": "https://linear.app/team/LIN-42",
        }
        task = PMClient.parse_linear_task(data)
        assert task.provider_id == "LIN-42"
        assert task.status == PMTaskStatus.IN_PROGRESS
        assert task.assignee == "Bob"
        assert task.priority == "Urgent"
        assert task.labels == ("perf", "backend")
        assert task.url == "https://linear.app/team/LIN-42"

    def test_done_state(self) -> None:
        data = {"id": "1", "title": "t", "description": "", "state": {"name": "Done"}}
        assert PMClient.parse_linear_task(data).status == PMTaskStatus.DONE

    def test_missing_optional_fields(self) -> None:
        data = {"id": "1", "title": "t", "description": "d"}
        task = PMClient.parse_linear_task(data)
        assert task.assignee is None
        assert task.priority is None
        assert task.labels == ()


# ---------------------------------------------------------------------------
# Tests — parse_asana_task
# ---------------------------------------------------------------------------


class TestParseAsanaTask:
    def test_completed_task(self) -> None:
        data = {"gid": "111", "name": "Deploy", "notes": "prod deploy", "completed": True}
        task = PMClient.parse_asana_task(data)
        assert task.status == PMTaskStatus.DONE
        assert task.url == "https://app.asana.com/0/0/111"

    def test_in_progress_section(self) -> None:
        data = {
            "gid": "222",
            "name": "Review",
            "notes": "",
            "completed": False,
            "memberships": [{"section": {"name": "In Progress"}}],
        }
        task = PMClient.parse_asana_task(data)
        assert task.status == PMTaskStatus.IN_PROGRESS

    def test_todo_default(self) -> None:
        data = {"gid": "333", "name": "Plan", "notes": "", "completed": False}
        task = PMClient.parse_asana_task(data)
        assert task.status == PMTaskStatus.TODO

    def test_tags_parsed(self) -> None:
        data = {
            "gid": "444",
            "name": "t",
            "notes": "",
            "completed": False,
            "tags": [{"name": "urgent"}, {"name": "frontend"}],
        }
        task = PMClient.parse_asana_task(data)
        assert task.labels == ("urgent", "frontend")

    def test_assignee_parsed(self) -> None:
        data = {
            "gid": "555",
            "name": "t",
            "notes": "",
            "completed": False,
            "assignee": {"name": "Carol"},
        }
        task = PMClient.parse_asana_task(data)
        assert task.assignee == "Carol"


# ---------------------------------------------------------------------------
# Tests — parse_shortcut_story
# ---------------------------------------------------------------------------


class TestParseShortcutStory:
    def test_completed_story(self) -> None:
        data = {
            "id": 100,
            "name": "Ship v2",
            "description": "Release v2",
            "completed": True,
            "started": True,
            "story_type": "feature",
            "app_url": "https://app.shortcut.com/story/100",
        }
        task = PMClient.parse_shortcut_story(data)
        assert task.status == PMTaskStatus.DONE
        assert task.priority == "feature"
        assert task.url == "https://app.shortcut.com/story/100"

    def test_started_story(self) -> None:
        data = {
            "id": 200,
            "name": "WIP",
            "description": "",
            "completed": False,
            "started": True,
            "story_type": "bug",
        }
        task = PMClient.parse_shortcut_story(data)
        assert task.status == PMTaskStatus.IN_PROGRESS

    def test_unstarted_story(self) -> None:
        data = {
            "id": 300,
            "name": "Backlog",
            "description": "",
            "completed": False,
            "started": False,
            "story_type": "",
        }
        task = PMClient.parse_shortcut_story(data)
        assert task.status == PMTaskStatus.TODO
        assert task.priority is None

    def test_owner_ids_parsed(self) -> None:
        data = {
            "id": 400,
            "name": "t",
            "description": "",
            "completed": False,
            "started": False,
            "story_type": "",
            "owner_ids": ["usr-1", "usr-2"],
        }
        task = PMClient.parse_shortcut_story(data)
        assert task.assignee == "usr-1"

    def test_labels_parsed(self) -> None:
        data = {
            "id": 500,
            "name": "t",
            "description": "",
            "completed": False,
            "started": False,
            "story_type": "",
            "labels": [{"name": "p1"}, {"name": "infra"}],
        }
        task = PMClient.parse_shortcut_story(data)
        assert task.labels == ("p1", "infra")


# ---------------------------------------------------------------------------
# Tests — convert_bernstein_status
# ---------------------------------------------------------------------------


class TestConvertBernsteinStatus:
    @pytest.mark.parametrize(
        ("bernstein_status", "expected"),
        [
            ("open", PMTaskStatus.TODO),
            ("pending", PMTaskStatus.TODO),
            ("in_progress", PMTaskStatus.IN_PROGRESS),
            ("running", PMTaskStatus.IN_PROGRESS),
            ("done", PMTaskStatus.DONE),
            ("completed", PMTaskStatus.DONE),
            ("failed", PMTaskStatus.DONE),
        ],
    )
    def test_known_statuses(self, bernstein_status: str, expected: PMTaskStatus) -> None:
        assert convert_bernstein_status(bernstein_status) == expected

    def test_case_insensitive(self) -> None:
        assert convert_bernstein_status("DONE") == PMTaskStatus.DONE
        assert convert_bernstein_status("Open") == PMTaskStatus.TODO

    def test_strips_whitespace(self) -> None:
        assert convert_bernstein_status("  done  ") == PMTaskStatus.DONE

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown Bernstein status"):
            convert_bernstein_status("nonexistent")


# ---------------------------------------------------------------------------
# Tests — render_sync_report
# ---------------------------------------------------------------------------


class TestRenderSyncReport:
    def test_basic_report(self) -> None:
        result = PMSyncResult(created=3, updated=5, skipped=2)
        report = render_sync_report(result)
        assert "## PM Sync Report" in report
        assert "| Created | 3 |" in report
        assert "| Updated | 5 |" in report
        assert "| Skipped | 2 |" in report
        assert "| **Total** | **10** |" in report

    def test_report_with_errors(self) -> None:
        result = PMSyncResult(created=0, updated=0, skipped=0, errors=("timeout", "auth failed"))
        report = render_sync_report(result)
        assert "### Errors (2)" in report
        assert "- timeout" in report
        assert "- auth failed" in report

    def test_report_no_errors_section_when_empty(self) -> None:
        result = PMSyncResult(created=1, updated=0, skipped=0)
        report = render_sync_report(result)
        assert "Errors" not in report
