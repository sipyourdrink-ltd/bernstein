"""Tests for the McpFakeLab harness (T602).

Validates that the lab correctly drives every Bernstein MCP tool via the
in-memory transport, and that it catches representative protocol regressions.
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.mcp_fake_lab import McpFakeLab

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def lab() -> McpFakeLab:
    """Fresh McpFakeLab for each test."""
    return McpFakeLab()


# ---------------------------------------------------------------------------
# bernstein_run
# ---------------------------------------------------------------------------


class TestBernsteinRun:
    """McpFakeLab correctly exercises bernstein_run."""

    @pytest.mark.asyncio
    async def test_run_returns_task_id(self, lab: McpFakeLab) -> None:
        """bernstein_run returns a JSON string containing a task_id."""
        text = await lab.call_tool("bernstein_run", {"goal": "Build auth"})
        data = json.loads(text)
        assert "task_id" in data
        assert data["task_id"].startswith("fake-")

    @pytest.mark.asyncio
    async def test_run_posts_to_tasks_endpoint(self, lab: McpFakeLab) -> None:
        """bernstein_run must POST to /tasks — catches regressions in the HTTP call."""
        await lab.call_tool("bernstein_run", {"goal": "Build auth"})
        lab.assert_called("POST", "/tasks")

    @pytest.mark.asyncio
    async def test_run_sends_goal_as_title(self, lab: McpFakeLab) -> None:
        """The goal is sent as the task title in the request body."""
        await lab.call_tool("bernstein_run", {"goal": "Refactor login flow"})
        post_req = next(r for r in lab.requests if r.method == "POST" and r.url.path == "/tasks")
        body = json.loads(post_req.content)
        assert body["title"] == "Refactor login flow"

    @pytest.mark.asyncio
    async def test_run_uses_provided_role(self, lab: McpFakeLab) -> None:
        """bernstein_run forwards the role arg to the task server."""
        await lab.call_tool("bernstein_run", {"goal": "Write tests", "role": "qa"})
        post_req = next(r for r in lab.requests if r.method == "POST")
        body = json.loads(post_req.content)
        assert body["role"] == "qa"

    @pytest.mark.asyncio
    async def test_run_persists_task_in_lab(self, lab: McpFakeLab) -> None:
        """After bernstein_run, the fake lab holds the created task."""
        await lab.call_tool("bernstein_run", {"goal": "Deploy pipeline"})
        assert len(lab._tasks) == 1
        task = next(iter(lab._tasks.values()))
        assert task["status"] == "open"


# ---------------------------------------------------------------------------
# bernstein_status
# ---------------------------------------------------------------------------


class TestBernsteinStatus:
    """McpFakeLab correctly exercises bernstein_status."""

    @pytest.mark.asyncio
    async def test_status_reflects_seeded_data(self, lab: McpFakeLab) -> None:
        """bernstein_status returns the seeded status payload."""
        lab.seed_status(total=7, open=3, done=4)
        text = await lab.call_tool("bernstein_status", {})
        data = json.loads(text)
        assert data["total"] == 7
        assert data["open"] == 3

    @pytest.mark.asyncio
    async def test_status_queries_status_endpoint(self, lab: McpFakeLab) -> None:
        """bernstein_status must GET /status — catches endpoint regressions."""
        await lab.call_tool("bernstein_status", {})
        lab.assert_called("GET", "/status")

    @pytest.mark.asyncio
    async def test_status_default_all_zeros(self, lab: McpFakeLab) -> None:
        """Without seeding, status has all-zero counts."""
        text = await lab.call_tool("bernstein_status", {})
        data = json.loads(text)
        assert data["total"] == 0
        assert data["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# bernstein_tasks
# ---------------------------------------------------------------------------


class TestBernsteinTasks:
    """McpFakeLab correctly exercises bernstein_tasks."""

    @pytest.mark.asyncio
    async def test_tasks_lists_seeded_tasks(self, lab: McpFakeLab) -> None:
        """bernstein_tasks returns all seeded tasks when no filter is given."""
        lab.seed_task("T-001", title="Alpha")
        lab.seed_task("T-002", title="Beta")
        text = await lab.call_tool("bernstein_tasks", {})
        data = json.loads(text)
        ids = {t["id"] for t in data}
        assert ids == {"T-001", "T-002"}

    @pytest.mark.asyncio
    async def test_tasks_filters_by_status(self, lab: McpFakeLab) -> None:
        """bernstein_tasks passes status=open to /tasks and returns only matching tasks."""
        lab.seed_task("T-001", status="open")
        lab.seed_task("T-002", status="done")
        text = await lab.call_tool("bernstein_tasks", {"status": "open"})
        data = json.loads(text)
        assert all(t["status"] == "open" for t in data)
        # Verify the query param was actually sent
        get_req = next(r for r in lab.requests if r.method == "GET" and r.url.path == "/tasks")
        assert get_req.url.params.get("status") == "open"

    @pytest.mark.asyncio
    async def test_tasks_empty_by_default(self, lab: McpFakeLab) -> None:
        """Without seeding, bernstein_tasks returns an empty list."""
        text = await lab.call_tool("bernstein_tasks", {})
        data = json.loads(text)
        assert data == []


# ---------------------------------------------------------------------------
# bernstein_cost
# ---------------------------------------------------------------------------


class TestBernsteinCost:
    """McpFakeLab correctly exercises bernstein_cost."""

    @pytest.mark.asyncio
    async def test_cost_returns_total_cost(self, lab: McpFakeLab) -> None:
        """bernstein_cost reads total_cost_usd from /status."""
        lab.seed_status(total_cost_usd=1.23, per_role=[{"role": "qa", "cost_usd": 0.50}])
        text = await lab.call_tool("bernstein_cost", {})
        data = json.loads(text)
        assert data["total_cost_usd"] == pytest.approx(1.23)

    @pytest.mark.asyncio
    async def test_cost_includes_per_role_breakdown(self, lab: McpFakeLab) -> None:
        """bernstein_cost exposes per-role cost data."""
        lab.seed_status(
            total_cost_usd=0.75,
            per_role=[{"role": "backend", "cost_usd": 0.50}, {"role": "qa", "cost_usd": 0.25}],
        )
        text = await lab.call_tool("bernstein_cost", {})
        data = json.loads(text)
        roles = {r["role"] for r in data["per_role"]}
        assert "backend" in roles
        assert "qa" in roles


# ---------------------------------------------------------------------------
# bernstein_approve
# ---------------------------------------------------------------------------


class TestBernsteinApprove:
    """McpFakeLab correctly exercises bernstein_approve."""

    @pytest.mark.asyncio
    async def test_approve_marks_task_done(self, lab: McpFakeLab) -> None:
        """bernstein_approve calls /tasks/{id}/complete and returns done status."""
        lab.seed_task("T-100", status="open")
        text = await lab.call_tool("bernstein_approve", {"task_id": "T-100"})
        data = json.loads(text)
        assert data["status"] == "done"
        assert lab._tasks["T-100"]["status"] == "done"

    @pytest.mark.asyncio
    async def test_approve_records_note(self, lab: McpFakeLab) -> None:
        """bernstein_approve stores the approval note as result_summary."""
        lab.seed_task("T-101", status="open")
        await lab.call_tool("bernstein_approve", {"task_id": "T-101", "note": "LGTM"})
        assert lab._tasks["T-101"]["result_summary"] == "LGTM"

    @pytest.mark.asyncio
    async def test_approve_posts_to_complete_endpoint(self, lab: McpFakeLab) -> None:
        """bernstein_approve must POST to /tasks/{id}/complete — regression guard."""
        lab.seed_task("T-102", status="open")
        await lab.call_tool("bernstein_approve", {"task_id": "T-102"})
        lab.assert_called("POST", "/tasks/T-102/complete")

    @pytest.mark.asyncio
    async def test_approve_missing_task_raises(self, lab: McpFakeLab) -> None:
        """bernstein_approve raises when the task does not exist in the fake server.

        FastMCP wraps underlying exceptions in ToolError; the 404 message is
        present in the error string.
        """
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="404"):
            await lab.call_tool("bernstein_approve", {"task_id": "no-such-task"})


# ---------------------------------------------------------------------------
# Regression guards — harness catches protocol drift
# ---------------------------------------------------------------------------


class TestRegressionGuards:
    """The harness detects representative regressions in the MCP tool protocol."""

    @pytest.mark.asyncio
    async def test_regression_run_must_not_skip_post(self, lab: McpFakeLab) -> None:
        """Regression: if bernstein_run stops POSTing to /tasks, assert_called catches it.

        This test documents the regression pattern: a future change that
        removes the HTTP call (e.g. returns a hard-coded response) would
        fail assert_called() because no POST request would be recorded.
        """
        await lab.call_tool("bernstein_run", {"goal": "Regression test task"})
        # Would raise AssertionError if the tool skips the POST
        lab.assert_called("POST", "/tasks")

    @pytest.mark.asyncio
    async def test_regression_tasks_filter_must_reach_server(self, lab: McpFakeLab) -> None:
        """Regression: status filter must be sent as a query param to /tasks.

        If bernstein_tasks filters client-side instead of passing ?status=X to
        the server, the query param assertion below catches the regression.
        """
        lab.seed_task("T-open", status="open")
        lab.seed_task("T-done", status="done")
        await lab.call_tool("bernstein_tasks", {"status": "open"})

        get_req = next(r for r in lab.requests if r.method == "GET" and r.url.path == "/tasks")
        assert get_req.url.params.get("status") == "open", (
            "Regression: bernstein_tasks must pass status as a server-side query param"
        )

    @pytest.mark.asyncio
    async def test_harness_assert_called_raises_on_missing_request(
        self, lab: McpFakeLab
    ) -> None:
        """The harness itself raises AssertionError when an expected call never happens."""
        # Don't make any calls — assert_called should detect the absence
        with pytest.raises(AssertionError, match="Expected POST /tasks"):
            lab.assert_called("POST", "/tasks")

    @pytest.mark.asyncio
    async def test_harness_assert_not_called_raises_on_unexpected_request(
        self, lab: McpFakeLab
    ) -> None:
        """The harness raises AssertionError when a forbidden call was made."""
        await lab.call_tool("bernstein_status", {})
        with pytest.raises(AssertionError, match="Expected no GET /status"):
            lab.assert_not_called("GET", "/status")
