"""Tests for run_changelog — agent-produced diff changelog generation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.run_changelog import (
    RunChangelog,
    TaskChange,
    _component_for_file,
    _component_for_role,
    _dominant_component,
    _is_breaking_diff,
    _make_summary,
    _task_link,
    format_markdown,
    generate_run_changelog,
)


# ---------------------------------------------------------------------------
# Component detection
# ---------------------------------------------------------------------------


class TestComponentForFile:
    def test_cli_module(self) -> None:
        assert _component_for_file("src/bernstein/cli/changelog_cmd.py") == "CLI"

    def test_routes(self) -> None:
        assert _component_for_file("src/bernstein/core/routes/tasks.py") == "API Routes"

    def test_core(self) -> None:
        assert _component_for_file("src/bernstein/core/models.py") == "Core Engine"

    def test_adapters(self) -> None:
        assert _component_for_file("src/bernstein/adapters/claude.py") == "Adapters"

    def test_templates(self) -> None:
        assert _component_for_file("templates/roles/backend.md") == "Role Templates"

    def test_tests(self) -> None:
        assert _component_for_file("tests/unit/test_foo.py") == "Unit Tests"

    def test_docs(self) -> None:
        assert _component_for_file("docs/roadmap.md") == "Documentation"

    def test_github(self) -> None:
        assert _component_for_file(".github/workflows/ci.yml") == "CI/CD"

    def test_unknown(self) -> None:
        assert _component_for_file("random/file.py") == "Other"


class TestDominantComponent:
    def test_picks_most_common(self) -> None:
        files = [
            "src/bernstein/cli/a.py",
            "src/bernstein/cli/b.py",
            "src/bernstein/core/c.py",
        ]
        assert _dominant_component(files) == "CLI"

    def test_empty_returns_default(self) -> None:
        assert _dominant_component([]) == "Other"


# ---------------------------------------------------------------------------
# Breaking change detection
# ---------------------------------------------------------------------------


class TestIsBreakingDiff:
    def test_removed_public_def(self) -> None:
        diff = "-def public_function(x: int) -> str:\n+    pass\n"
        assert _is_breaking_diff(diff)

    def test_removed_private_not_breaking(self) -> None:
        diff = "-def _private_function(x: int) -> str:\n+    pass\n"
        assert not _is_breaking_diff(diff)

    def test_breaking_change_footer(self) -> None:
        diff = "some diff\nBREAKING CHANGE: removed parameter\n"
        assert _is_breaking_diff(diff)

    def test_removed_route_decorator(self) -> None:
        diff = '-@router.get("/tasks")\n+@router.get("/v2/tasks")\n'
        assert _is_breaking_diff(diff)

    def test_normal_diff_not_breaking(self) -> None:
        diff = "+def new_function():\n+    pass\n"
        assert not _is_breaking_diff(diff)


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------


class TestMakeSummary:
    def test_conventional_commit_feat(self) -> None:
        summary = _make_summary("Add something", ["feat(cli): add run-changelog command"], [])
        assert summary == "Add add run-changelog command"

    def test_conventional_commit_fix(self) -> None:
        summary = _make_summary("Fix bug", ["fix(core): handle null task_id"], [])
        assert summary == "Fix handle null task_id"

    def test_falls_back_to_title(self) -> None:
        summary = _make_summary("Implement token monitoring", [], [])
        assert summary == "Implement token monitoring"

    def test_capitalises_title(self) -> None:
        summary = _make_summary("add feature", [], [])
        assert summary == "Add feature"

    def test_falls_back_to_files(self) -> None:
        files = ["src/bernstein/core/foo.py", "src/bernstein/core/bar.py"]
        summary = _make_summary("", [], files)
        assert "foo.py" in summary

    def test_last_resort(self) -> None:
        summary = _make_summary("", [], [])
        assert summary == "Miscellaneous change"


# ---------------------------------------------------------------------------
# Task link
# ---------------------------------------------------------------------------


class TestTaskLink:
    def test_with_repo_url(self) -> None:
        link = _task_link("abc12345xyz", "https://github.com/owner/repo")
        assert "#abc12345" in link
        assert "https://github.com/owner/repo" in link

    def test_without_repo_url(self) -> None:
        link = _task_link("abc12345xyz", None)
        assert "#abc12345" in link
        assert "http" not in link


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _make_changelog(**kwargs: object) -> RunChangelog:
    defaults: dict[str, object] = {
        "generated_at": time.time(),
        "since_ref": "v1.0.0",
        "tasks_total": 5,
        "changes": {},
        "breaking_changes": [],
    }
    defaults.update(kwargs)
    return RunChangelog(**defaults)  # type: ignore[arg-type]


class TestFormatMarkdown:
    def test_header_present(self) -> None:
        cl = _make_changelog()
        md = format_markdown(cl)
        assert "# Run Changelog" in md

    def test_empty_changes_message(self) -> None:
        cl = _make_changelog()
        md = format_markdown(cl)
        assert "No agent-produced changes" in md

    def test_breaking_change_section(self) -> None:
        change = TaskChange(
            task_id="abc12345",
            title="Remove API",
            role="backend",
            result_summary="",
            component="Core Engine",
            is_breaking=True,
            summary="Remove deprecated endpoint",
        )
        cl = _make_changelog(
            changes={"Core Engine": [change]},
            breaking_changes=[change],
        )
        md = format_markdown(cl)
        assert "Breaking Changes" in md
        assert "Remove deprecated endpoint" in md

    def test_component_section(self) -> None:
        change = TaskChange(
            task_id="def67890",
            title="Add feature",
            role="cli",
            result_summary="",
            component="CLI",
            summary="Add run-changelog command",
        )
        cl = _make_changelog(changes={"CLI": [change]})
        md = format_markdown(cl)
        assert "## CLI" in md
        assert "Add run-changelog command" in md

    def test_result_summary_included(self) -> None:
        change = TaskChange(
            task_id="xyz11111",
            title="Fix thing",
            role="backend",
            result_summary="Fixed the null pointer in orchestrator",
            component="Core Engine",
            summary="Fix null pointer",
        )
        cl = _make_changelog(changes={"Core Engine": [change]})
        md = format_markdown(cl)
        assert "Fixed the null pointer" in md

    def test_task_id_in_output(self) -> None:
        change = TaskChange(
            task_id="abc12345def6",
            title="Add feature",
            role="cli",
            result_summary="",
            component="CLI",
            summary="Add feature",
        )
        cl = _make_changelog(changes={"CLI": [change]})
        md = format_markdown(cl)
        assert "#abc12345" in md


# ---------------------------------------------------------------------------
# Component-for-role fallback
# ---------------------------------------------------------------------------


class TestComponentForRole:
    def test_backend_maps_to_core(self) -> None:
        assert _component_for_role("backend") == "Core Engine"

    def test_qa_maps_to_tests(self) -> None:
        assert _component_for_role("qa") == "Tests"

    def test_devops_maps_to_cicd(self) -> None:
        assert _component_for_role("devops") == "CI/CD"

    def test_unknown_role(self) -> None:
        assert _component_for_role("unknown") == "Other"


# ---------------------------------------------------------------------------
# Integration: generate_run_changelog with mocked git + server
# ---------------------------------------------------------------------------


class TestGenerateRunChangelog:
    def test_empty_when_no_tasks(self, tmp_path: Path) -> None:
        # No task server, no metrics files → empty changelog
        cl = generate_run_changelog(tmp_path, server_url="http://localhost:19999")
        assert cl.tasks_total == 0
        assert cl.changes == {}
        assert cl.breaking_changes == []

    def test_reads_tasks_from_metrics(self, tmp_path: Path) -> None:
        # Set up a fake metrics directory
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        record = {
            "schema_version": 1,
            "timestamp": time.time(),
            "task_id": "aabbccdd1234",
            "role": "backend",
            "success": True,
        }
        (metrics_dir / "2026-04-11.jsonl").write_text(json.dumps(record) + "\n")

        # No real git repo → commits_for_task returns []
        # include_no_commits=True so we see the task
        cl = generate_run_changelog(
            tmp_path,
            server_url="http://localhost:19999",
            include_no_commits=True,
        )
        assert cl.tasks_total >= 1

    def test_since_ref_respected(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)

        # No metrics → still works with since_ref
        cl = generate_run_changelog(
            tmp_path,
            server_url="http://localhost:19999",
            since_ref="v0.1.0",
        )
        assert cl.since_ref == "v0.1.0"
