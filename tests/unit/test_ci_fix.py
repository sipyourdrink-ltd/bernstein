"""Tests for bernstein.core.ci_fix and related CI modules."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.ci_fix import (
    CIFailure,
    CIFailureKind,
    CIFixAttempt,
    CIFixPipeline,
    CIFixResult,
    build_task_payload,
    install_pre_push_hook,
    parse_failures,
    write_ci_fix_task,
)


class TestParseFailures:
    def test_detects_ruff_lint(self) -> None:
        log = "E501 Line too long (130 > 120)\n  --> src/bernstein/core/foo.py:12:1"
        failures = parse_failures(log, job="lint")
        kinds = {f.kind for f in failures}
        assert CIFailureKind.RUFF_LINT in kinds

    def test_detects_ruff_format(self) -> None:
        log = "Would reformat: src/bernstein/core/bar.py\n2 files would be reformatted"
        failures = parse_failures(log, job="lint")
        kinds = {f.kind for f in failures}
        assert CIFailureKind.RUFF_FORMAT in kinds

    def test_detects_missing_file(self) -> None:
        log = "FileNotFoundError: templates/prompts/judge.md not found"
        failures = parse_failures(log, job="test")
        kinds = {f.kind for f in failures}
        assert CIFailureKind.MISSING_FILE in kinds

    def test_detects_import_error(self) -> None:
        log = "ModuleNotFoundError: No module named 'ruff'\n  test_foo.py"
        failures = parse_failures(log, job="test")
        kinds = {f.kind for f in failures}
        assert CIFailureKind.IMPORT_ERROR in kinds

    def test_detects_pytest_failure(self) -> None:
        log = "FAILED tests/unit/test_foo.py::test_bar - AssertionError\npytest exit code 1"
        failures = parse_failures(log, job="test")
        kinds = {f.kind for f in failures}
        assert CIFailureKind.PYTEST in kinds

    def test_unknown_for_empty_log(self) -> None:
        failures = parse_failures("", job="test")
        assert len(failures) == 1
        assert failures[0].kind == CIFailureKind.UNKNOWN

    def test_extracts_affected_files(self) -> None:
        log = "E501 Line too long\n  --> src/bernstein/core/fast_path.py:10"
        failures = parse_failures(log, job="lint")
        lint = next(f for f in failures if f.kind == CIFailureKind.RUFF_LINT)
        assert "src/bernstein/core/fast_path.py" in lint.affected_files

    def test_fix_hint_provided(self) -> None:
        log = "Would reformat: src/bernstein/foo.py"
        failures = parse_failures(log, job="lint")
        fmt = next(f for f in failures if f.kind == CIFailureKind.RUFF_FORMAT)
        assert "ruff format" in fmt.fix_hint


class TestBuildTaskPayload:
    def test_title_contains_failure_kinds(self) -> None:
        log = "E501 Line too long\n  --> src/bernstein/core/x.py:1"
        failures = parse_failures(log, job="lint")
        payload = build_task_payload(failures)
        assert "ci-fix" in payload["title"].lower()

    def test_role_is_qa(self) -> None:
        failures = parse_failures("Would reformat: src/bernstein/x.py", job="lint")
        payload = build_task_payload(failures)
        assert payload["role"] == "qa"

    def test_priority_is_1(self) -> None:
        failures = parse_failures("FAILED tests/unit/test_foo.py::test_bar\npytest", job="test")
        payload = build_task_payload(failures)
        assert payload["priority"] == 1

    def test_run_url_in_description(self) -> None:
        failures = parse_failures("E501 Line too long\n  --> src/x.py:1", "lint")
        payload = build_task_payload(failures, run_url="https://github.com/foo/bar/actions/runs/1")
        assert "https://github.com/foo/bar/actions/runs/1" in payload["description"]


class TestWriteCiFixTask:
    def test_creates_json_file(self, tmp_path: Path) -> None:
        backlog_dir = tmp_path / "open"
        failures = parse_failures("E501 Line too long\n  --> src/bernstein/x.py:1", "lint")
        path = write_ci_fix_task(backlog_dir, failures)
        assert path.exists()
        assert path.suffix == ".json"

    def test_task_has_required_fields(self, tmp_path: Path) -> None:
        import json

        backlog_dir = tmp_path / "open"
        failures = parse_failures("Would reformat: src/bernstein/x.py", "lint")
        path = write_ci_fix_task(backlog_dir, failures)
        data = json.loads(path.read_text())
        assert "id" in data
        assert data["status"] == "open"
        assert data["priority"] == 1

    def test_creates_dir_if_missing(self, tmp_path: Path) -> None:
        backlog_dir = tmp_path / "nested" / "open"
        failures = parse_failures("E501 Line too long\n  --> src/bernstein/x.py:1", "lint")
        write_ci_fix_task(backlog_dir, failures)
        assert backlog_dir.exists()


class TestInstallPrePushHook:
    def test_installs_hook(self, tmp_path: Path) -> None:
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        result = install_pre_push_hook(tmp_path)
        assert result is True
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        assert hook.exists()
        assert "ruff" in hook.read_text()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix file permissions not applicable on Windows")
    def test_hook_is_executable(self, tmp_path: Path) -> None:
        import stat

        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        install_pre_push_hook(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        mode = hook.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_skips_existing_without_force(self, tmp_path: Path) -> None:
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        hook = git_hooks / "pre-push"
        hook.write_text("# existing hook")
        result = install_pre_push_hook(tmp_path, force=False)
        assert result is False
        assert hook.read_text() == "# existing hook"

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        hook = git_hooks / "pre-push"
        hook.write_text("# existing hook")
        result = install_pre_push_hook(tmp_path, force=True)
        assert result is True
        assert "ruff" in hook.read_text()

    @pytest.mark.skipif(
        not (Path(".git") / "hooks").exists(),
        reason="no .git/hooks in test environment",
    )
    def test_real_repo_install_then_restore(self, tmp_path: Path) -> None:
        # Just ensure the function signature works; don't mutate the real repo.
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        install_pre_push_hook(tmp_path)


# ---------------------------------------------------------------------------
# CIFixPipeline tests
# ---------------------------------------------------------------------------

_RUFF_LOG = "E501 Line too long (130 > 120)\n  --> src/bernstein/core/foo.py:12:1"
_EMPTY_LOG = ""


class TestCIFixPipeline:
    def test_run_from_log_creates_task(self, tmp_path: Path) -> None:
        pipeline = CIFixPipeline(backlog_dir=tmp_path)
        attempts = pipeline.run_from_log(_RUFF_LOG, run_url="https://example.com")
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.TASK_CREATED
        assert attempts[0].failures
        assert attempts[0].task_id  # non-empty

    def test_run_from_log_no_failures(self, tmp_path: Path) -> None:
        pipeline = CIFixPipeline(backlog_dir=tmp_path)
        # An empty log produces UNKNOWN with empty details -> filtered.
        attempts = pipeline.run_from_log(_EMPTY_LOG)
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.NO_FAILURES
        assert not attempts[0].failures

    def test_run_from_url_download_error(self) -> None:
        pipeline = CIFixPipeline()
        with patch(
            "bernstein.core.ci_fix.download_github_actions_log",
            side_effect=RuntimeError("gh not found"),
        ):
            attempts = pipeline.run_from_url("https://github.com/o/r/actions/runs/1")
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.DOWNLOAD_ERROR
        assert "gh not found" in attempts[0].error

    def test_run_from_url_success(self, tmp_path: Path) -> None:
        pipeline = CIFixPipeline(backlog_dir=tmp_path)
        with patch(
            "bernstein.core.ci_fix.download_github_actions_log",
            return_value=_RUFF_LOG,
        ):
            attempts = pipeline.run_from_url("https://github.com/o/r/actions/runs/1")
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.TASK_CREATED

    def test_retry_limit_enforcement(self, tmp_path: Path) -> None:
        pipeline = CIFixPipeline(max_retries=2, backlog_dir=tmp_path)
        attempts = pipeline.run_loop(_RUFF_LOG)
        # Should have 2 TASK_CREATED + 1 MAX_RETRIES sentinel.
        task_attempts = [a for a in attempts if a.result == CIFixResult.TASK_CREATED]
        max_attempts = [a for a in attempts if a.result == CIFixResult.MAX_RETRIES]
        assert len(task_attempts) == 2
        assert len(max_attempts) == 1

    def test_run_loop_stops_on_no_failures(self, tmp_path: Path) -> None:
        pipeline = CIFixPipeline(max_retries=5, backlog_dir=tmp_path)
        attempts = pipeline.run_loop(_EMPTY_LOG)
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.NO_FAILURES

    def test_custom_parser(self, tmp_path: Path) -> None:
        """Pipeline uses a custom parser when provided."""

        class StubParser:
            name = "stub"

            def parse(self, raw_log: str) -> list[CIFailure]:
                return [
                    CIFailure(
                        kind=CIFailureKind.RUFF_LINT,
                        job="stub-job",
                        summary="stub failure",
                        details="stub details",
                    )
                ]

        pipeline = CIFixPipeline(parser=StubParser(), backlog_dir=tmp_path)
        attempts = pipeline.run_from_log("anything")
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.TASK_CREATED
        assert attempts[0].failures[0].job == "stub-job"

    def test_backlog_dir_creates_file(self, tmp_path: Path) -> None:
        backlog = tmp_path / "open"
        pipeline = CIFixPipeline(backlog_dir=backlog)
        pipeline.run_from_log(_RUFF_LOG)
        files = list(backlog.glob("*.json"))
        assert len(files) >= 1

    def test_server_post_fallback(self) -> None:
        """When server POST fails and no backlog dir, task_id is empty."""
        pipeline = CIFixPipeline(server_url="http://127.0.0.1:99999")
        with patch("bernstein.core.quality.ci_fix.post_ci_fix_task", return_value=False):
            attempts = pipeline.run_from_log(_RUFF_LOG)
        assert len(attempts) == 1
        assert attempts[0].result == CIFixResult.TASK_CREATED
        assert attempts[0].task_id == ""


class TestCIFixAttemptDataclass:
    def test_default_timestamp(self) -> None:
        attempt = CIFixAttempt(attempt=1, failures=[], result=CIFixResult.NO_FAILURES)
        assert attempt.timestamp > 0

    def test_fields_populated(self) -> None:
        attempt = CIFixAttempt(
            attempt=2,
            failures=[],
            result=CIFixResult.DOWNLOAD_ERROR,
            task_id="ci-fix-123",
            error="timeout",
        )
        assert attempt.attempt == 2
        assert attempt.task_id == "ci-fix-123"
        assert attempt.error == "timeout"


# ---------------------------------------------------------------------------
# GitHub Actions parser tests
# ---------------------------------------------------------------------------


class TestGitHubActionsParser:
    def test_parse_grouped_log(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser

        log = (
            "2024-01-15T10:30:00.0000000Z ##[group]Run ruff check src/\n"
            "2024-01-15T10:30:01.0000000Z src/foo.py:10:1: E302 Expected 2 blank lines\n"
            "2024-01-15T10:30:01.0000000Z Found 3 errors.\n"
            "2024-01-15T10:30:01.0000000Z ##[error]Process completed with exit code 1.\n"
            "2024-01-15T10:30:01.0000000Z ##[endgroup]\n"
        )
        parser = GitHubActionsParser()
        failures = parser.parse(log)
        assert len(failures) >= 1
        kinds = {f.kind for f in failures}
        assert CIFailureKind.RUFF_LINT in kinds

    def test_parse_format_errors(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser

        log = (
            "2024-01-15T10:30:00.0000000Z ##[group]Run ruff format --check\n"
            "2024-01-15T10:30:01.0000000Z Would reformat: src/bernstein/core/bar.py\n"
            "2024-01-15T10:30:01.0000000Z ##[error]Process completed with exit code 1.\n"
            "2024-01-15T10:30:01.0000000Z ##[endgroup]\n"
        )
        parser = GitHubActionsParser()
        failures = parser.parse(log)
        kinds = {f.kind for f in failures}
        assert CIFailureKind.RUFF_FORMAT in kinds

    def test_parse_pytest_failure(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser

        log = (
            "2024-01-15T10:30:00.0000000Z ##[group]Run pytest\n"
            "2024-01-15T10:30:01.0000000Z FAILED tests/unit/test_foo.py::test_bar\n"
            "2024-01-15T10:30:01.0000000Z pytest: 1 failed, 10 passed\n"
            "2024-01-15T10:30:01.0000000Z ##[error]Process completed with exit code 1.\n"
            "2024-01-15T10:30:01.0000000Z ##[endgroup]\n"
        )
        parser = GitHubActionsParser()
        failures = parser.parse(log)
        kinds = {f.kind for f in failures}
        assert CIFailureKind.PYTEST in kinds

    def test_parse_no_groups_fallback(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser

        log = (
            "2024-01-15T10:30:00.0000000Z E501 Line too long\n"
            "2024-01-15T10:30:01.0000000Z src/bernstein/core/x.py:1:1\n"
        )
        parser = GitHubActionsParser()
        failures = parser.parse(log)
        assert len(failures) >= 1
        kinds = {f.kind for f in failures}
        assert CIFailureKind.RUFF_LINT in kinds

    def test_strip_timestamps(self) -> None:
        from bernstein.adapters.ci.github_actions import _strip_timestamps

        line = "2024-01-15T10:30:00.0000000Z some content"
        assert _strip_timestamps(line) == "some content"

    def test_extract_run_id(self) -> None:
        from bernstein.adapters.ci.github_actions import _extract_run_id

        url = "https://github.com/owner/repo/actions/runs/12345678"
        assert _extract_run_id(url) == "12345678"

    def test_extract_run_id_invalid(self) -> None:
        from bernstein.adapters.ci.github_actions import _extract_run_id

        with pytest.raises(ValueError, match="Cannot extract run ID"):
            _extract_run_id("https://github.com/owner/repo")

    def test_parser_satisfies_protocol(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser
        from bernstein.core.ci_log_parser import CILogParser

        parser = GitHubActionsParser()
        assert isinstance(parser, CILogParser)


# ---------------------------------------------------------------------------
# CI log parser registry tests
# ---------------------------------------------------------------------------


class TestCILogParserRegistry:
    def test_register_and_get(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser
        from bernstein.core.ci_log_parser import get_parser, register_parser

        parser = GitHubActionsParser()
        register_parser(parser)
        retrieved = get_parser("github_actions")
        assert retrieved is parser

    def test_get_unknown_returns_none(self) -> None:
        from bernstein.core.ci_log_parser import get_parser

        assert get_parser("nonexistent_ci_system") is None

    def test_list_parsers(self) -> None:
        from bernstein.adapters.ci.github_actions import GitHubActionsParser
        from bernstein.core.ci_log_parser import list_parsers, register_parser

        register_parser(GitHubActionsParser())
        names = list_parsers()
        assert "github_actions" in names


class TestGHADownload:
    def test_download_gh_cli_success(self) -> None:
        from bernstein.adapters.ci.github_actions import download_github_actions_log

        with patch("bernstein.adapters.ci.github_actions.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "log output here"
            result = download_github_actions_log("https://github.com/o/r/actions/runs/123")
        assert result == "log output here"
        mock_run.assert_called_once()

    def test_download_gh_cli_failure(self) -> None:
        from bernstein.adapters.ci.github_actions import download_github_actions_log

        with patch("bernstein.adapters.ci.github_actions.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error: not authenticated"
            with pytest.raises(RuntimeError, match="gh run view failed"):
                download_github_actions_log("https://github.com/o/r/actions/runs/123")

    def test_download_api_invalid_url_no_run_id(self) -> None:
        from bernstein.adapters.ci.github_actions import (
            download_github_actions_log_api,
        )

        with pytest.raises(ValueError, match="Cannot extract run ID"):
            download_github_actions_log_api("https://not-github.com/foo")

    def test_download_api_invalid_url_no_repo(self) -> None:
        from bernstein.adapters.ci.github_actions import (
            download_github_actions_log_api,
        )

        # Has a run ID but not a github.com URL, so owner/repo extraction fails.
        with pytest.raises(ValueError, match="Cannot parse owner/repo"):
            download_github_actions_log_api("https://not-github.com/foo/actions/runs/123")
