"""Tests for ci_monitor — CIMonitor, FailureContext parsing, CIAutofixPipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bernstein.core.ci_monitor import (
    CIFailure,
    CIMonitor,
    FailureContext,
    parse_log_to_context,
)

# ---------------------------------------------------------------------------
# Sample log fixtures
# ---------------------------------------------------------------------------

PYTEST_FAILURE_LOG = """\
============================= FAILURES =============================
FAILED tests/unit/test_router.py::test_route_small_task
  File "src/bernstein/core/router.py", line 42
    return model_map[key]
KeyError: 'nonexistent'

Traceback (most recent call last):
  File "src/bernstein/core/router.py", line 42, in route
    return model_map[key]
KeyError: 'nonexistent'

============================= short test summary ===================
FAILED tests/unit/test_router.py::test_route_small_task
"""

RUFF_FAILURE_LOG = """\
src/bernstein/core/router.py:42:5: E302 Expected 2 blank lines, found 1
Found 3 errors.
"""

EMPTY_LOG = ""

GENERIC_ERROR_LOG = """\
Error: something broke unexpectedly
Some additional context about the failure
"""


# ---------------------------------------------------------------------------
# TestParseLogToContext
# ---------------------------------------------------------------------------


class TestParseLogToContext:
    """Tests for the ``parse_log_to_context`` function."""

    def test_pytest_failure(self) -> None:
        ctx = parse_log_to_context(PYTEST_FAILURE_LOG)

        assert ctx.test_name == "tests/unit/test_router.py::test_route_small_task"
        assert "KeyError" in ctx.error_message
        assert ctx.file_path == "src/bernstein/core/router.py"
        assert ctx.line_number == 42
        assert "Traceback" in ctx.stack_trace

    def test_generic_error(self) -> None:
        ctx = parse_log_to_context(GENERIC_ERROR_LOG)

        assert "something broke" in ctx.error_message
        assert ctx.test_name == ""

    def test_empty_log(self) -> None:
        ctx = parse_log_to_context(EMPTY_LOG)

        assert ctx.error_message == "Unknown CI failure"
        assert ctx.test_name == ""
        assert ctx.file_path == ""
        assert ctx.line_number == 0
        assert ctx.stack_trace == ""

    def test_ruff_failure_no_traceback(self) -> None:
        ctx = parse_log_to_context(RUFF_FAILURE_LOG)

        assert ctx.test_name == ""
        assert ctx.stack_trace == ""


# ---------------------------------------------------------------------------
# TestCIFailure dataclass
# ---------------------------------------------------------------------------


class TestCIFailureDataclass:
    """Tests for the ``CIFailure`` frozen dataclass."""

    def test_create(self) -> None:
        f = CIFailure(
            run_id=12345,
            workflow_name="CI",
            branch="main",
            commit_sha="abc123",
            failure_url="https://github.com/o/r/actions/runs/12345",
            timestamp="2026-03-30T10:00:00Z",
        )
        assert f.run_id == 12345
        assert f.workflow_name == "CI"
        assert f.branch == "main"

    def test_frozen(self) -> None:
        f = CIFailure(
            run_id=1,
            workflow_name="CI",
            branch="main",
            commit_sha="abc",
            failure_url="",
            timestamp="",
        )
        with pytest.raises(AttributeError):
            f.run_id = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestCIMonitor
# ---------------------------------------------------------------------------


class TestCIMonitorPollFailures:
    """Tests for ``CIMonitor.poll_failures`` with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_poll_returns_failures(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.json.return_value = {
            "workflow_runs": [
                {
                    "id": 100,
                    "name": "CI",
                    "head_branch": "feature-x",
                    "head_sha": "deadbeef",
                    "html_url": "https://github.com/o/r/actions/runs/100",
                    "created_at": "2026-03-30T12:00:00Z",
                },
                {
                    "id": 101,
                    "name": "Deploy",
                    "head_branch": "main",
                    "head_sha": "cafebabe",
                    "html_url": "https://github.com/o/r/actions/runs/101",
                    "created_at": "2026-03-30T12:05:00Z",
                },
            ]
        }
        mock_response.raise_for_status = MagicMock()

        monitor = CIMonitor()

        with patch("bernstein.core.ci_monitor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            failures = await monitor.poll_failures("o/r", "fake-token")

        assert len(failures) == 2
        assert failures[0].run_id == 100
        assert failures[0].workflow_name == "CI"
        assert failures[1].run_id == 101
        assert failures[1].branch == "main"

    @pytest.mark.asyncio
    async def test_poll_deduplicates_seen_runs(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.json.return_value = {
            "workflow_runs": [
                {
                    "id": 200,
                    "name": "CI",
                    "head_branch": "main",
                    "head_sha": "aaa",
                    "html_url": "",
                    "created_at": "",
                },
            ]
        }
        mock_response.raise_for_status = MagicMock()

        monitor = CIMonitor()
        monitor.seen_run_ids.add(200)

        with patch("bernstein.core.ci_monitor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            failures = await monitor.poll_failures("o/r", "fake-token")

        assert len(failures) == 0

    @pytest.mark.asyncio
    async def test_poll_empty_response(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.json.return_value = {"workflow_runs": []}
        mock_response.raise_for_status = MagicMock()

        monitor = CIMonitor()

        with patch("bernstein.core.ci_monitor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            failures = await monitor.poll_failures("o/r", "fake-token")

        assert failures == []


class TestCIMonitorParseFailureLogs:
    """Tests for ``CIMonitor.parse_failure_logs`` with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_parse_failure_logs(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = PYTEST_FAILURE_LOG
        mock_response.raise_for_status = MagicMock()

        monitor = CIMonitor()

        with patch("bernstein.core.ci_monitor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            ctx = await monitor.parse_failure_logs("o/r", 999, "fake-token")

        assert ctx.test_name == "tests/unit/test_router.py::test_route_small_task"
        assert "KeyError" in ctx.error_message
        assert ctx.file_path == "src/bernstein/core/router.py"
        assert ctx.line_number == 42


# ---------------------------------------------------------------------------
# TestCIAutofixPipeline
# ---------------------------------------------------------------------------


class TestCIAutofixPipelineCreateTask:
    """Tests for ``CIAutofixPipeline.create_fix_task``."""

    def test_create_fix_task_success(self) -> None:
        from bernstein.core.ci_fix import CIAutofixPipeline

        failure = FailureContext(
            test_name="tests/test_foo.py::test_bar",
            error_message="AssertionError: expected 1 got 2",
            file_path="src/foo.py",
            line_number=10,
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "task-abc123"}
        mock_response.raise_for_status = MagicMock()

        pipeline = CIAutofixPipeline(server_url="http://test:8052")

        with patch("httpx.post", return_value=mock_response) as mock_post:
            task_id = pipeline.create_fix_task(failure, run_url="https://github.com/o/r/actions/runs/1")

        assert task_id == "task-abc123"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "[ci-autofix]" in payload["title"]
        assert "AssertionError" in payload["description"]
        assert payload["priority"] == 1
        assert payload["role"] == "qa"

    def test_create_fix_task_server_error(self) -> None:
        from bernstein.core.ci_fix import CIAutofixPipeline

        failure = FailureContext(
            test_name="tests/test_x.py::test_y",
            error_message="TypeError",
        )

        pipeline = CIAutofixPipeline(server_url="http://test:8052")

        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            task_id = pipeline.create_fix_task(failure)

        assert task_id == ""


class TestCIAutofixPipelineCreatePR:
    """Tests for ``CIAutofixPipeline.create_fix_pr``."""

    def test_create_fix_pr_success(self, tmp_path: pytest.TempPathFactory) -> None:
        from bernstein.core.ci_fix import CIAutofixPipeline
        from bernstein.core.git_pr import PullRequestResult

        failure = FailureContext(
            test_name="tests/test_a.py::test_b",
            error_message="ValueError: bad input",
            file_path="src/a.py",
            line_number=5,
        )

        pipeline = CIAutofixPipeline(server_url="http://test:8052")
        mock_result = PullRequestResult(
            success=True,
            pr_url="https://github.com/o/r/pull/42",
        )

        with patch("bernstein.core.git_pr.create_github_pr", return_value=mock_result) as mock_pr:
            pr_url = pipeline.create_fix_pr(
                "task-xyz",
                failure,
                cwd=tmp_path,  # type: ignore[arg-type]
            )

        assert pr_url == "https://github.com/o/r/pull/42"
        mock_pr.assert_called_once()

    def test_create_fix_pr_no_cwd(self) -> None:
        from bernstein.core.ci_fix import CIAutofixPipeline

        failure = FailureContext(
            test_name="",
            error_message="err",
        )
        pipeline = CIAutofixPipeline(server_url="http://test:8052")
        pr_url = pipeline.create_fix_pr("task-1", failure)
        assert pr_url == ""
