"""Tests for the backlog-to-server sync module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from bernstein.core.sync import (
    BacklogTask,
    SyncResult,
    _file_to_slug,
    _task_already_exists,
    normalise_title,
    parse_backlog_file,
    sync_backlog_to_server,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_md(path: Path, content: str) -> None:
    """Write content to path, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


SAMPLE_MD_BOLD = """\
# Wire TierAwareRouter into orchestrator

**Role:** backend
**Priority:** 2 (normal)
**Scope:** medium
**Complexity:** medium

## Problem

The router is unused at runtime.
"""

SAMPLE_MD_BOLD_CRITICAL = """\
# Decompose evolution module

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Overview

Big refactor needed.
"""

SAMPLE_MD_YAML_FRONTMATTER = """\
---
title: Add golden benchmark suite
role: qa
priority: 2
scope: medium
complexity: medium
---

# Add golden benchmark suite

Tests that verify baseline performance.
"""

SAMPLE_MD_NO_TITLE = """\
**Role:** backend
**Priority:** 2

Some content without a heading.
"""


# ---------------------------------------------------------------------------
# parse_backlog_file
# ---------------------------------------------------------------------------


class TestParseBacklogFile:
    def test_parses_markdown_bold_format(self, tmp_path: Path) -> None:
        p = tmp_path / "115-wire-router.md"
        p.write_text(SAMPLE_MD_BOLD, encoding="utf-8")

        task = parse_backlog_file(p)

        assert task is not None
        assert task.title == "Wire TierAwareRouter into orchestrator"
        assert task.role == "backend"
        assert task.priority == 2
        assert task.scope == "medium"
        assert task.complexity == "medium"
        assert task.source_file == "115-wire-router.md"

    def test_parses_critical_priority(self, tmp_path: Path) -> None:
        p = tmp_path / "100-decompose.md"
        p.write_text(SAMPLE_MD_BOLD_CRITICAL, encoding="utf-8")

        task = parse_backlog_file(p)

        assert task is not None
        assert task.priority == 1
        assert task.scope == "large"
        assert task.complexity == "high"
        assert task.role == "architect"

    def test_parses_yaml_frontmatter(self, tmp_path: Path) -> None:
        p = tmp_path / "107-golden-benchmarks.md"
        p.write_text(SAMPLE_MD_YAML_FRONTMATTER, encoding="utf-8")

        task = parse_backlog_file(p)

        assert task is not None
        assert task.title == "Add golden benchmark suite"
        assert task.role == "qa"
        assert task.priority == 2

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")

        assert parse_backlog_file(p) is None

    def test_returns_none_when_no_title(self, tmp_path: Path) -> None:
        p = tmp_path / "notitle.md"
        p.write_text(SAMPLE_MD_NO_TITLE, encoding="utf-8")

        assert parse_backlog_file(p) is None

    def test_description_contains_full_text(self, tmp_path: Path) -> None:
        p = tmp_path / "115-wire-router.md"
        p.write_text(SAMPLE_MD_BOLD, encoding="utf-8")

        task = parse_backlog_file(p)

        assert task is not None
        assert "router is unused" in task.description

    def test_defaults_role_to_backend(self, tmp_path: Path) -> None:
        p = tmp_path / "norole.md"
        p.write_text("# Some Task\n\nDescription here.\n", encoding="utf-8")

        task = parse_backlog_file(p)

        assert task is not None
        assert task.role == "backend"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.md"

        assert parse_backlog_file(p) is None


# ---------------------------------------------------------------------------
# normalise_title / _file_to_slug
# ---------------------------------------------------------------------------


class TestNormaliseTitle:
    def test_lowercases(self) -> None:
        assert normalise_title("Wire TierAwareRouter") == "wire-tierawarerouter"

    def test_replaces_spaces_with_hyphens(self) -> None:
        assert normalise_title("Add golden benchmark suite") == "add-golden-benchmark-suite"

    def test_strips_special_characters(self) -> None:
        assert normalise_title("Fix: auth/session bug!") == "fix-auth-session-bug"

    def test_identical_on_same_input(self) -> None:
        title = "Decompose evolution module"
        assert normalise_title(title) == normalise_title(title)


class TestFileToSlug:
    def test_strips_numeric_prefix(self) -> None:
        assert _file_to_slug("115-wire-tier-aware-router.md") == "wire-tier-aware-router"

    def test_strips_md_extension(self) -> None:
        assert _file_to_slug("some-task.md") == "some-task"

    def test_no_prefix(self) -> None:
        assert _file_to_slug("task.md") == "task"


class TestTaskAlreadyExists:
    def _make_task(self, title: str, source_file: str = "001-task.md") -> BacklogTask:
        return BacklogTask(
            title=title,
            description="",
            role="backend",
            priority=2,
            scope="medium",
            complexity="medium",
            source_file=source_file,
        )

    def test_matches_by_normalised_title(self) -> None:
        task = self._make_task("Wire TierAwareRouter into spawner")
        slugs = {normalise_title("Wire TierAwareRouter into spawner")}

        assert _task_already_exists(task, slugs) is True

    def test_no_match_on_different_title(self) -> None:
        task = self._make_task("Build new feature")
        slugs = {normalise_title("Completely different task")}

        assert _task_already_exists(task, slugs) is False

    def test_empty_slugs_returns_false(self) -> None:
        task = self._make_task("Some task")
        assert _task_already_exists(task, set()) is False

    def test_matches_file_slug_exactly(self) -> None:
        task = self._make_task("Unrelated title", source_file="115-wire-tier-aware-router.md")
        slugs = {"wire-tier-aware-router"}

        assert _task_already_exists(task, slugs) is True


# ---------------------------------------------------------------------------
# sync_backlog_to_server — integration with mock httpx
# ---------------------------------------------------------------------------


def _make_mock_transport(
    responses: dict[str, httpx.Response],
) -> httpx.MockTransport:
    """Build a transport that returns canned responses keyed by 'METHOD /path'."""
    created_tasks: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"

        # Dynamic POST /tasks handler
        if request.method == "POST" and url.path == "/tasks":
            body = json.loads(request.content)
            task_id = f"T-{len(created_tasks) + 1:03d}"
            task = {**body, "id": task_id, "status": "open"}
            created_tasks.append(task)
            return httpx.Response(201, json=task)

        if key in responses:
            return responses[key]
        return httpx.Response(404, json={"detail": f"No mock for {key}"})

    return httpx.MockTransport(handler)


def _empty_server_transport() -> httpx.MockTransport:
    """Server with no existing tasks."""
    return _make_mock_transport(
        {
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=claimed": httpx.Response(200, json=[]),
            "GET /tasks?status=in_progress": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
            "GET /tasks?status=failed": httpx.Response(200, json=[]),
        }
    )


def _server_with_done_task(title: str) -> httpx.MockTransport:
    """Server where one task is already done."""
    closed_task = {
        "id": "T-done-1",
        "title": title,
        "description": "",
        "role": "backend",
        "status": "closed",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": "Done",
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
    }
    return _make_mock_transport(
        {
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=claimed": httpx.Response(200, json=[]),
            "GET /tasks?status=in_progress": httpx.Response(200, json=[]),
            "GET /tasks?status=closed": httpx.Response(200, json=[closed_task]),
            "GET /tasks?status=failed": httpx.Response(200, json=[]),
        }
    )


def _server_with_existing_task(title: str) -> httpx.MockTransport:
    """Server where one task already exists (open)."""
    existing = {
        "id": "T-existing",
        "title": title,
        "status": "open",
    }
    return _make_mock_transport(
        {
            "GET /tasks?status=open": httpx.Response(200, json=[existing]),
            "GET /tasks?status=claimed": httpx.Response(200, json=[]),
            "GET /tasks?status=in_progress": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
            "GET /tasks?status=failed": httpx.Response(200, json=[]),
        }
    )


class TestSyncBacklogToServer:
    def test_creates_task_for_new_md_file(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        _write_md(backlog_open / "115-wire-router.md", SAMPLE_MD_BOLD)

        transport = _empty_server_transport()
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert len(result.created) == 1
        assert len(result.errors) == 0
        assert len(result.skipped) == 0

    def test_skips_task_already_on_server(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        _write_md(backlog_open / "115-wire-router.md", SAMPLE_MD_BOLD)

        # Server already has a task with the same title
        transport = _server_with_existing_task("Wire TierAwareRouter into orchestrator")
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert len(result.created) == 0
        assert "115-wire-router.md" in result.skipped

    def test_moves_file_for_done_task(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        backlog_closed = tmp_path / ".sdd" / "backlog" / "closed"
        _write_md(backlog_open / "115-wire-router.md", SAMPLE_MD_BOLD)

        transport = _server_with_done_task("Wire TierAwareRouter into orchestrator")
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert "115-wire-router.md" in result.moved
        assert not (backlog_open / "115-wire-router.md").exists()
        assert (backlog_closed / "115-wire-router.md").exists()

    def test_returns_empty_result_when_no_backlog(self, tmp_path: Path) -> None:
        # No .sdd/backlog/open/ directory at all
        transport = _empty_server_transport()
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert result == SyncResult()

    def test_handles_empty_backlog_dir(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        backlog_open.mkdir(parents=True)

        transport = _empty_server_transport()
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert result.created == []
        assert result.errors == []

    def test_creates_multiple_tasks(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        _write_md(backlog_open / "100-task-a.md", SAMPLE_MD_BOLD)
        _write_md(backlog_open / "101-task-b.md", SAMPLE_MD_BOLD_CRITICAL)

        transport = _empty_server_transport()
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert len(result.created) == 2
        assert len(result.errors) == 0

    def test_skips_unparseable_file(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        _write_md(backlog_open / "bad.md", "No heading here, just text.")

        transport = _empty_server_transport()
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        # unparseable file should go to errors (title extracted to empty)
        # actually "No heading here, just text." has no h1 heading
        assert result.created == []

    def test_no_duplicate_when_created_in_same_run(self, tmp_path: Path) -> None:
        """Two files with same title should only create one task."""
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        same_title_content = "# Same Task Title\n\n**Role:** backend\n"
        _write_md(backlog_open / "001-same.md", same_title_content)
        _write_md(backlog_open / "002-same.md", same_title_content)

        transport = _empty_server_transport()
        client = httpx.Client(transport=transport, base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        # Second file should be skipped as duplicate in-run
        assert len(result.created) == 1
        assert len(result.skipped) == 1

    def test_error_on_server_unreachable(self, tmp_path: Path) -> None:
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        _write_md(backlog_open / "115-task.md", SAMPLE_MD_BOLD)

        def always_fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = httpx.Client(transport=httpx.MockTransport(always_fail), base_url="http://testserver")

        result = sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert len(result.errors) == 1
        assert "Cannot connect" in result.errors[0]

    def test_description_embeds_source_filename(self, tmp_path: Path) -> None:
        """Task description should contain '<!-- source: filename.md -->'."""
        backlog_open = tmp_path / ".sdd" / "backlog" / "open"
        _write_md(backlog_open / "115-wire-router.md", SAMPLE_MD_BOLD)

        captured_payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "POST" and url.path == "/tasks/batch":
                return httpx.Response(404, json={"detail": "not found"})
            if request.method == "POST" and url.path == "/tasks":
                captured_payloads.append(json.loads(request.content))
                return httpx.Response(201, json={"id": "T-001", "status": "open"})
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")

        sync_backlog_to_server(tmp_path, "http://testserver", client=client)

        assert len(captured_payloads) == 1
        assert "source: 115-wire-router.md" in captured_payloads[0]["description"]
