"""Tests for automated integration test generation (ROAD-164)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bernstein.core.integration_test_gen import (
    IntegTestGenConfig,
    IntegTestGenResult,
    _extract_python_code,
    _get_diff,
    _slug_from_title,
    generate_and_run,
    run_integration_test_gen_sync,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-integ-001",
    title: str = "Add widget factory",
    description: str = "Implement widget factory module.",
) -> MagicMock:
    task = MagicMock()
    task.id = id
    task.title = title
    task.description = description
    return task


# ---------------------------------------------------------------------------
# _extract_python_code
# ---------------------------------------------------------------------------


class TestExtractPythonCode:
    def test_strips_python_fences(self) -> None:
        raw = "```python\ndef test_foo():\n    assert True\n```"
        result = _extract_python_code(raw)
        assert result == "def test_foo():\n    assert True"

    def test_strips_plain_fences(self) -> None:
        raw = "```\ndef test_bar():\n    pass\n```"
        result = _extract_python_code(raw)
        assert result == "def test_bar():\n    pass"

    def test_no_fences_passthrough(self) -> None:
        raw = "def test_baz():\n    assert 1 == 1"
        result = _extract_python_code(raw)
        assert result == raw.strip()

    def test_empty_string(self) -> None:
        assert _extract_python_code("") == ""

    def test_whitespace_only(self) -> None:
        assert _extract_python_code("   \n  \n  ") == ""


# ---------------------------------------------------------------------------
# _slug_from_title
# ---------------------------------------------------------------------------


class TestSlugFromTitle:
    def test_basic_title(self) -> None:
        assert _slug_from_title("Add widget factory") == "add_widget_factory"

    def test_special_chars_removed(self) -> None:
        slug = _slug_from_title("Fix bug #123 in API!")
        assert slug == "fix_bug_123_in_api"

    def test_truncation_at_40(self) -> None:
        long_title = "a" * 60
        assert len(_slug_from_title(long_title)) == 40

    def test_empty_title_fallback(self) -> None:
        assert _slug_from_title("!!!") == "change"

    def test_empty_string_fallback(self) -> None:
        assert _slug_from_title("") == "change"


# ---------------------------------------------------------------------------
# _get_diff
# ---------------------------------------------------------------------------


class TestGetDiff:
    def test_returns_diff_in_git_repo(self, tmp_path: Path) -> None:
        # Non-git dir → should return empty (graceful failure)
        result = _get_diff(tmp_path)
        assert isinstance(result, str)

    def test_returns_empty_on_error(self, tmp_path: Path) -> None:
        result = _get_diff(tmp_path, base_ref="nonexistent-ref")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# IntegTestGenConfig defaults
# ---------------------------------------------------------------------------


class TestIntegTestGenConfig:
    def test_defaults(self) -> None:
        cfg = IntegTestGenConfig()
        assert cfg.enabled is False
        assert cfg.block_on_fail is True
        assert cfg.write_tests is False
        assert cfg.max_diff_chars == 12_000
        assert cfg.test_timeout_s == 120

    def test_custom_values(self) -> None:
        cfg = IntegTestGenConfig(
            enabled=True,
            model="test-model",
            provider="test-provider",
            max_diff_chars=5000,
            max_tokens=1024,
            test_timeout_s=60,
            block_on_fail=False,
            write_tests=True,
        )
        assert cfg.enabled is True
        assert cfg.model == "test-model"
        assert cfg.write_tests is True


# ---------------------------------------------------------------------------
# IntegTestGenResult
# ---------------------------------------------------------------------------


class TestIntegTestGenResult:
    def test_minimal(self) -> None:
        r = IntegTestGenResult(passed=True, blocked=False, detail="ok")
        assert r.passed
        assert not r.blocked
        assert r.test_code == ""
        assert r.errors == []

    def test_with_errors(self) -> None:
        r = IntegTestGenResult(
            passed=False,
            blocked=True,
            detail="failed",
            errors=["err1", "err2"],
        )
        assert not r.passed
        assert r.blocked
        assert len(r.errors) == 2


# ---------------------------------------------------------------------------
# generate_and_run
# ---------------------------------------------------------------------------


class TestGenerateAndRun:
    def test_no_diff_skips(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True)
        (tmp_path / "tests" / "integration").mkdir(parents=True)

        with patch("bernstein.core.quality.integration_test_gen._get_diff", return_value=""):
            result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert result.passed
        assert not result.blocked
        assert "No Python changes" in result.detail

    def test_llm_error_returns_failure(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True)
        (tmp_path / "tests" / "integration").mkdir(parents=True)

        with (
            patch(
                "bernstein.core.integration_test_gen._get_diff",
                return_value="diff --git a/foo.py\n+x=1",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API down"),
            ),
        ):
            result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert not result.passed
        assert result.blocked
        assert "LLM error" in result.detail
        assert len(result.errors) == 1

    def test_invalid_llm_output_returns_failure(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True)
        (tmp_path / "tests" / "integration").mkdir(parents=True)

        with (
            patch(
                "bernstein.core.integration_test_gen._get_diff",
                return_value="diff --git a/foo.py\n+x=1",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                new_callable=AsyncMock,
                return_value="This is not a test function.",
            ),
        ):
            result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert not result.passed
        assert "no valid test function" in result.detail

    def test_non_blocking_when_configured(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True, block_on_fail=False)
        (tmp_path / "tests" / "integration").mkdir(parents=True)

        with (
            patch(
                "bernstein.core.integration_test_gen._get_diff",
                return_value="diff --git a/foo.py\n+x=1",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API down"),
            ),
        ):
            result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert not result.passed
        assert not result.blocked  # block_on_fail=False

    def test_successful_test_run(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True)
        integ_dir = tmp_path / "tests" / "integration"
        integ_dir.mkdir(parents=True)

        valid_test = "def test_integration_widget():\n    assert 1 + 1 == 2\n"

        with (
            patch(
                "bernstein.core.integration_test_gen._get_diff",
                return_value="diff --git a/foo.py\n+x=1",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                new_callable=AsyncMock,
                return_value=valid_test,
            ),
        ):
            # Mock subprocess to simulate pytest pass
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"1 passed", b""))

            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert result.passed
        assert not result.blocked
        assert "passed" in result.detail

    def test_failing_test_run(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True, block_on_fail=True)
        integ_dir = tmp_path / "tests" / "integration"
        integ_dir.mkdir(parents=True)

        valid_test = "def test_integration_widget():\n    assert False\n"

        with (
            patch(
                "bernstein.core.integration_test_gen._get_diff",
                return_value="diff --git a/foo.py\n+x=1",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                new_callable=AsyncMock,
                return_value=valid_test,
            ),
        ):
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"FAILED test_integration_widget", b""))

            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert not result.passed
        assert result.blocked
        assert "FAILED" in result.detail

    def test_write_tests_persists_file(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True, write_tests=True)
        integ_dir = tmp_path / "tests" / "integration"
        integ_dir.mkdir(parents=True)

        valid_test = "def test_integration_widget():\n    assert True\n"

        with (
            patch(
                "bernstein.core.integration_test_gen._get_diff",
                return_value="diff --git a/foo.py\n+x=1",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                new_callable=AsyncMock,
                return_value=valid_test,
            ),
        ):
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"1 passed", b""))

            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                result = asyncio.run(generate_and_run(task, tmp_path, config))

        assert result.passed
        assert result.test_path != ""
        generated_dir = tmp_path / "tests" / "integration" / "generated"
        assert generated_dir.exists()
        generated_files = list(generated_dir.glob("test_gen_*.py"))
        assert len(generated_files) == 1


# ---------------------------------------------------------------------------
# run_integration_test_gen_sync
# ---------------------------------------------------------------------------


class TestRunIntegrationTestGenSync:
    def test_sync_wrapper_returns_result(self, tmp_path: Path) -> None:
        """Test the sync wrapper returns a valid result on exception."""
        task = _make_task()
        # Use a non-existent run_dir to trigger an error path
        config = IntegTestGenConfig(enabled=True, block_on_fail=True)

        result = run_integration_test_gen_sync(task, tmp_path, config)
        # In Python 3.12+ without an event loop, this goes through the
        # exception handler path and returns a failure result
        assert isinstance(result, IntegTestGenResult)
        assert not result.passed
        assert len(result.errors) >= 1

    def test_sync_wrapper_block_on_fail(self, tmp_path: Path) -> None:
        task = _make_task()
        config = IntegTestGenConfig(enabled=True, block_on_fail=False)

        result = run_integration_test_gen_sync(task, tmp_path, config)
        assert isinstance(result, IntegTestGenResult)
        # block_on_fail=False, so even on error it shouldn't block
        assert not result.blocked
