"""Tests for bernstein.core.ci_fix."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.ci_fix import (
    CIFailureKind,
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
        assert "https://github.com" in payload["description"]


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
