"""Unit tests for workflow_importer: detect, parse, and import task files.

Tests cover TODO.md / TASKS.md detection, checkbox parsing, duplicate
skipping, and the import flow against a mocked task server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.workflow_importer import (
    detect_workflow_files,
    import_workflow_tasks,
    parse_markdown_tasks,
)


class TestDetectWorkflowFiles:
    def test_detects_todo_md(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("# TODO\n")
        found = detect_workflow_files(tmp_path)
        assert any(f.name == "TODO.md" for f in found)

    def test_detects_tasks_md(self, tmp_path: Path) -> None:
        (tmp_path / "TASKS.md").write_text("# Tasks\n")
        found = detect_workflow_files(tmp_path)
        assert any(f.name == "TASKS.md" for f in found)

    def test_detects_dot_plan(self, tmp_path: Path) -> None:
        (tmp_path / ".plan").write_text("- [ ] do something\n")
        found = detect_workflow_files(tmp_path)
        assert any(f.name == ".plan" for f in found)

    def test_returns_empty_when_no_files(self, tmp_path: Path) -> None:
        assert detect_workflow_files(tmp_path) == []

    def test_does_not_scan_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "TODO.md").write_text("# todo\n")
        found = detect_workflow_files(tmp_path)
        assert found == []

    def test_detects_multiple_files(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("")
        (tmp_path / "TASKS.md").write_text("")
        found = detect_workflow_files(tmp_path)
        names = {f.name for f in found}
        assert {"TODO.md", "TASKS.md"} == names


class TestParseMarkdownTasks:
    def test_parses_unchecked_items(self) -> None:
        content = "- [ ] Do the thing\n- [ ] Fix the bug\n"
        tasks = parse_markdown_tasks(content)
        assert tasks == ["Do the thing", "Fix the bug"]

    def test_ignores_checked_items(self) -> None:
        content = "- [x] Already done\n- [X] Also done\n- [ ] Still pending\n"
        tasks = parse_markdown_tasks(content)
        assert tasks == ["Still pending"]

    def test_handles_asterisk_bullet(self) -> None:
        content = "* [ ] Another task\n"
        tasks = parse_markdown_tasks(content)
        assert tasks == ["Another task"]

    def test_ignores_plain_text_lines(self) -> None:
        content = "# Heading\n\nSome paragraph.\n\n- [ ] Real task\n"
        tasks = parse_markdown_tasks(content)
        assert tasks == ["Real task"]

    def test_empty_content_returns_empty_list(self) -> None:
        assert parse_markdown_tasks("") == []

    def test_strips_leading_trailing_whitespace_from_titles(self) -> None:
        content = "-  [ ]   Trimmed title   \n"
        tasks = parse_markdown_tasks(content)
        assert tasks == ["Trimmed title"]

    def test_indented_items_are_parsed(self) -> None:
        content = "  - [ ] Indented task\n"
        tasks = parse_markdown_tasks(content)
        assert tasks == ["Indented task"]


class TestImportWorkflowTasks:
    def _mock_client(self, existing_titles: list[str] | None = None) -> MagicMock:
        """Build a mock httpx.Client that simulates the task server."""
        client = MagicMock()

        # GET /tasks response
        tasks_payload = [{"title": t} for t in (existing_titles or [])]
        get_resp = MagicMock()
        get_resp.is_success = True
        get_resp.json.return_value = {"tasks": tasks_payload, "total": len(tasks_payload)}
        client.get.return_value = get_resp

        # POST /tasks response
        post_resp = MagicMock()
        post_resp.raise_for_status = MagicMock()
        client.post.return_value = post_resp

        return client

    def test_imports_unchecked_items(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [ ] Fix auth bug\n- [ ] Write docs\n")
        client = self._mock_client()
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")
        assert count == 2
        assert client.post.call_count == 2

    def test_skips_duplicate_titles(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [ ] Fix auth bug\n")
        client = self._mock_client(existing_titles=["fix auth bug"])  # already present
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")
        assert count == 0
        client.post.assert_not_called()

    def test_dry_run_does_not_post(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [ ] Refactor module\n")
        client = self._mock_client()
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052", dry_run=True)
        assert count == 1
        client.post.assert_not_called()

    def test_returns_zero_when_no_workflow_files(self, tmp_path: Path) -> None:
        client = self._mock_client()
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")
        assert count == 0
        client.get.assert_not_called()
        client.post.assert_not_called()

    def test_imports_from_multiple_files(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [ ] Task A\n")
        (tmp_path / "TASKS.md").write_text("- [ ] Task B\n")
        client = self._mock_client()
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")
        assert count == 2

    def test_skips_checked_items(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [x] Done already\n- [ ] Still to do\n")
        client = self._mock_client()
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")
        assert count == 1

    def test_gracefully_handles_server_post_error(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [ ] Failing task\n")
        client = self._mock_client()
        client.post.side_effect = Exception("connection refused")
        # Should not raise; returns 0 imported
        count = import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")
        assert count == 0

    def test_payload_contains_source_metadata(self, tmp_path: Path) -> None:
        (tmp_path / "TODO.md").write_text("- [ ] Add tests\n")
        client = self._mock_client()
        import_workflow_tasks(tmp_path, client, "http://127.0.0.1:8052")

        call_args = client.post.call_args
        payload = call_args[1]["json"] if call_args[1] else call_args[0][1]
        assert payload["metadata"].get("workflow_import_source") == "TODO.md"
        assert payload["title"] == "Add tests"
