"""Tests for CI failure auto-routing (334f).

Covers:
- bernstein.github_app.ci_router — git helpers, blame attribution, payload builder
- bernstein.github_app.mapper.workflow_run_to_task — mapper integration
- bernstein.core.routes.webhooks — workflow_run event handling + retry cap
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUFF_LOG = (
    "E501 Line too long (130 > 120)\n"
    "  --> src/bernstein/core/foo.py:12:1\n"
    "Found 3 errors.\n"
    "##[error]Process completed with exit code 1.\n"
)


def _make_workflow_run_event(
    conclusion: str = "failure",
    action: str = "completed",
    head_sha: str = "abcdef12",
    head_branch: str = "main",
    workflow_name: str = "CI",
    run_url: str = "https://github.com/o/r/actions/runs/99",
) -> Any:
    """Build a minimal WebhookEvent-like object for workflow_run tests."""
    from bernstein.github_app.webhooks import WebhookEvent

    return WebhookEvent(
        event_type="workflow_run",
        action=action,
        repo_full_name="o/r",
        sender="bot",
        payload={
            "workflow_run": {
                "conclusion": conclusion,
                "head_sha": head_sha,
                "head_branch": head_branch,
                "name": workflow_name,
                "html_url": run_url,
            }
        },
    )


# ---------------------------------------------------------------------------
# ci_router: git helpers
# ---------------------------------------------------------------------------


class TestGetCommitFiles:
    def test_returns_files_on_success(self, tmp_path: Path) -> None:
        from bernstein.github_app.ci_router import get_commit_files

        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "src/a.py\nsrc/b.py\n"
            result = get_commit_files("abc1234", cwd=tmp_path)
        assert "src/a.py" in result
        assert "src/b.py" in result

    def test_returns_empty_on_failure(self, tmp_path: Path) -> None:
        from bernstein.github_app.ci_router import get_commit_files

        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = get_commit_files("bad_sha", cwd=tmp_path)
        assert result == []

    def test_handles_os_error(self) -> None:
        from bernstein.github_app.ci_router import get_commit_files

        with patch(
            "bernstein.github_app.ci_router.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            assert get_commit_files("abc1234") == []


class TestGetCommitDiff:
    def test_returns_diff_on_success(self) -> None:
        from bernstein.github_app.ci_router import get_commit_diff

        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "diff --git a/foo.py b/foo.py\n+pass\n"
            result = get_commit_diff("abc1234")
        assert "diff" in result

    def test_returns_empty_on_failure(self) -> None:
        from bernstein.github_app.ci_router import get_commit_diff

        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            result = get_commit_diff("bad_sha")
        assert result == ""

    def test_truncates_to_max_chars(self) -> None:
        from bernstein.github_app import ci_router
        from bernstein.github_app.ci_router import get_commit_diff

        big_diff = "+" * (ci_router._DIFF_MAX_CHARS + 500)
        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = big_diff
            result = get_commit_diff("abc1234")
        assert len(result) == ci_router._DIFF_MAX_CHARS


class TestGetCommitMessage:
    def test_returns_subject(self) -> None:
        from bernstein.github_app.ci_router import get_commit_message

        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "feat: add new thing\n"
            result = get_commit_message("abc1234")
        assert result == "feat: add new thing"

    def test_returns_empty_on_failure(self) -> None:
        from bernstein.github_app.ci_router import get_commit_message

        with patch("bernstein.github_app.ci_router.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = get_commit_message("bad_sha")
        assert result == ""


# ---------------------------------------------------------------------------
# ci_router: blame attribution
# ---------------------------------------------------------------------------


class TestBlameCiFailures:
    def _make_failure(self, files: list[str]) -> Any:
        from bernstein.core.ci_fix import CIFailure, CIFailureKind

        return CIFailure(
            kind=CIFailureKind.RUFF_LINT,
            job="lint",
            summary="ruff lint errors",
            affected_files=files,
        )

    def test_direct_overlap(self) -> None:
        from bernstein.github_app.ci_router import blame_ci_failures

        failures = [self._make_failure(["src/bernstein/core/foo.py"])]
        with (
            patch(
                "bernstein.github_app.ci_router.get_commit_files",
                return_value=["src/bernstein/core/foo.py", "src/other.py"],
            ),
            patch("bernstein.github_app.ci_router.get_commit_diff", return_value="diff"),
            patch("bernstein.github_app.ci_router.get_commit_message", return_value="fix stuff"),
        ):
            blame = blame_ci_failures(failures, "abc1234")

        assert "src/bernstein/core/foo.py" in blame.responsible_files
        assert blame.diff_context == "diff"
        assert blame.commit_message == "fix stuff"

    def test_fallback_when_no_overlap(self) -> None:
        from bernstein.github_app.ci_router import blame_ci_failures

        # CI fails on foo.py, but the commit only changed bar.py
        failures = [self._make_failure(["src/bernstein/core/foo.py"])]
        with (
            patch(
                "bernstein.github_app.ci_router.get_commit_files",
                return_value=["src/other/bar.py"],
            ),
            patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
            patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
        ):
            blame = blame_ci_failures(failures, "abc1234")

        # Fallback: use the commit's changed files
        assert "src/other/bar.py" in blame.responsible_files

    def test_empty_failures_no_overlap(self) -> None:
        from bernstein.github_app.ci_router import blame_ci_failures

        with (
            patch(
                "bernstein.github_app.ci_router.get_commit_files",
                return_value=["src/a.py"],
            ),
            patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
            patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
        ):
            blame = blame_ci_failures([], "abc1234")

        # No failing_set intersection, falls back to commit files
        assert "src/a.py" in blame.responsible_files

    def test_empty_sha_returns_empty_blame(self) -> None:
        from bernstein.github_app.ci_router import CIBlameResult, blame_ci_failures

        with (
            patch("bernstein.github_app.ci_router.get_commit_files", return_value=[]),
            patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
            patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
        ):
            blame = blame_ci_failures([], "")

        assert isinstance(blame, CIBlameResult)


# ---------------------------------------------------------------------------
# ci_router: build_ci_routing_payload
# ---------------------------------------------------------------------------


class TestBuildCiRoutingPayload:
    def _make_blame(self) -> Any:
        from bernstein.github_app.ci_router import CIBlameResult

        return CIBlameResult(
            head_sha="abcdef12",
            responsible_files=["src/bernstein/core/foo.py"],
            diff_context="+# new line\n",
            commit_message="feat: break everything",
        )

    def _parse_failures(self) -> list[Any]:
        from bernstein.core.ci_fix import parse_failures

        return parse_failures(_RUFF_LOG, job="lint")

    def test_title_contains_sha_and_workflow(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "CI", retry_count=0)
        assert "abcdef12" in payload["title"]
        assert "CI" in payload["title"]

    def test_priority_is_1_and_role_qa(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "CI")
        assert payload["priority"] == 1
        assert payload["role"] == "qa"

    def test_first_attempt_uses_sonnet(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "CI", retry_count=0)
        assert payload["model"] == "sonnet"
        assert payload["effort"] == "high"

    def test_third_attempt_escalates_to_opus(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "CI", retry_count=2)
        assert payload["model"] == "opus"
        assert payload["effort"] == "max"

    def test_description_contains_diff(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "CI")
        assert "+# new line" in payload["description"]

    def test_description_contains_run_url(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(
            self._parse_failures(),
            self._make_blame(),
            "CI",
            run_url="https://github.com/o/r/actions/runs/1",
        )
        assert "https://github.com" in payload["description"]

    def test_retry_note_in_description(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "CI", retry_count=1)
        assert "Retry attempt" in payload["description"]

    def test_title_max_120_chars(self) -> None:
        from bernstein.github_app.ci_router import build_ci_routing_payload

        payload = build_ci_routing_payload(self._parse_failures(), self._make_blame(), "A" * 100, retry_count=0)
        assert len(payload["title"]) <= 120


# ---------------------------------------------------------------------------
# mapper: workflow_run_to_task
# ---------------------------------------------------------------------------


class TestWorkflowRunToTask:
    def test_ignores_non_workflow_run_event(self) -> None:
        from bernstein.github_app.mapper import workflow_run_to_task
        from bernstein.github_app.webhooks import WebhookEvent

        event = WebhookEvent(
            event_type="push",
            action="",
            repo_full_name="o/r",
            sender="bot",
            payload={},
        )
        assert workflow_run_to_task(event) == []

    def test_ignores_non_failure_conclusion(self) -> None:
        from bernstein.github_app.mapper import workflow_run_to_task

        event = _make_workflow_run_event(conclusion="success")
        assert workflow_run_to_task(event) == []

    def test_ignores_non_completed_action(self) -> None:
        from bernstein.github_app.mapper import workflow_run_to_task

        event = _make_workflow_run_event(action="requested")
        assert workflow_run_to_task(event) == []

    def test_returns_task_on_success(self) -> None:
        from bernstein.github_app.mapper import workflow_run_to_task

        event = _make_workflow_run_event()
        with (
            patch(
                "bernstein.adapters.ci.github_actions.download_github_actions_log",
                return_value=_RUFF_LOG,
            ),
            patch(
                "bernstein.github_app.ci_router.get_commit_files",
                return_value=["src/bernstein/core/foo.py"],
            ),
            patch(
                "bernstein.github_app.ci_router.get_commit_diff",
                return_value="diff --git ...\n+pass\n",
            ),
            patch(
                "bernstein.github_app.ci_router.get_commit_message",
                return_value="feat: break stuff",
            ),
        ):
            tasks = workflow_run_to_task(event)

        assert len(tasks) == 1
        task = tasks[0]
        assert task["role"] == "qa"
        assert task["priority"] == 1
        assert "[ci-fix]" in task["title"]

    def test_returns_empty_when_log_download_fails(self) -> None:
        from bernstein.github_app.mapper import workflow_run_to_task

        event = _make_workflow_run_event()
        with patch(
            "bernstein.adapters.ci.github_actions.download_github_actions_log",
            side_effect=RuntimeError("gh not found"),
        ):
            tasks = workflow_run_to_task(event)

        # No log → no parseable failures → empty list
        assert tasks == []

    def test_retry_count_escalates_model(self) -> None:
        from bernstein.github_app.mapper import workflow_run_to_task

        event = _make_workflow_run_event()
        with (
            patch(
                "bernstein.adapters.ci.github_actions.download_github_actions_log",
                return_value=_RUFF_LOG,
            ),
            patch("bernstein.github_app.ci_router.get_commit_files", return_value=[]),
            patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
            patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
        ):
            tasks = workflow_run_to_task(event, retry_count=2)

        assert len(tasks) == 1
        assert tasks[0]["model"] == "opus"


# ---------------------------------------------------------------------------
# webhooks route: workflow_run handling (async integration tests)
# ---------------------------------------------------------------------------

_WORKFLOW_RUN_FAILURE_PAYLOAD = {
    "action": "completed",
    "repository": {"full_name": "o/r"},
    "sender": {"login": "bot"},
    "workflow_run": {
        "conclusion": "failure",
        "head_sha": "abcdef12345678",
        "head_branch": "main",
        "name": "CI",
        "html_url": "https://github.com/o/r/actions/runs/99",
    },
}


@pytest.fixture()
def _jsonl(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def _app(_jsonl: Path) -> Any:
    from bernstein.core.server import create_app

    return create_app(jsonl_path=_jsonl)


@pytest.fixture()
async def _client(_app: Any) -> Any:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _wf_body(**overrides: Any) -> bytes:
    import json

    payload = {**_WORKFLOW_RUN_FAILURE_PAYLOAD}
    run = {**payload["workflow_run"], **overrides}  # type: ignore[dict-item]
    payload["workflow_run"] = run  # type: ignore[assignment]
    return json.dumps(payload).encode()


@pytest.mark.anyio
async def test_webhook_workflow_run_creates_task(_client: Any) -> None:
    body = _wf_body()
    with (
        patch(
            "bernstein.adapters.ci.github_actions.download_github_actions_log",
            return_value=_RUFF_LOG,
        ),
        patch("bernstein.github_app.ci_router.get_commit_files", return_value=[]),
        patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
        patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
    ):
        resp = await _client.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "workflow_run"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_created"] == 1
    assert data["event_type"] == "workflow_run"


@pytest.mark.anyio
async def test_webhook_workflow_run_success_no_task(_client: Any) -> None:
    body = _wf_body(conclusion="success")
    resp = await _client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "workflow_run"},
    )
    assert resp.status_code == 200
    assert resp.json()["tasks_created"] == 0


@pytest.mark.anyio
async def test_webhook_workflow_run_retry_cap_enforced(_client: Any) -> None:
    """After MAX_CI_RETRIES active ci-fix tasks, no new task is created."""
    import json

    from bernstein.github_app.ci_router import MAX_CI_RETRIES

    # Pre-seed the store with MAX_CI_RETRIES ci-fix tasks for branch "main"
    for _ in range(MAX_CI_RETRIES):
        seed_payload = {
            "title": "[ci-fix][abcdef12] CI: ruff_lint",
            "description": "branch main triggered this failure",
            "role": "qa",
            "priority": 1,
            "scope": "small",
            "task_type": "fix",
        }
        seed_resp = await _client.post(
            "/tasks",
            content=json.dumps(seed_payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert seed_resp.status_code == 201

    # Now trigger another CI failure for the same branch
    body = _wf_body()
    with (
        patch(
            "bernstein.adapters.ci.github_actions.download_github_actions_log",
            return_value=_RUFF_LOG,
        ),
        patch("bernstein.github_app.ci_router.get_commit_files", return_value=[]),
        patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
        patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
    ):
        resp = await _client.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "workflow_run"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_created"] == 0
    assert "max_retries_reached" in data.get("skipped_reason", "")


@pytest.mark.anyio
async def test_webhook_workflow_run_bad_json_returns_400(_client: Any) -> None:
    resp = await _client.post(
        "/webhooks/github",
        content=b"not json",
        headers={"X-GitHub-Event": "workflow_run"},
    )
    assert resp.status_code == 400
