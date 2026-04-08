"""Unit tests for integration_test_gen quality gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.integration_test_gen import (
    IntegTestGenConfig,
    _extract_python_code,
    _slug_from_title,
    generate_and_run,
)

# ---------------------------------------------------------------------------
# _extract_python_code
# ---------------------------------------------------------------------------


def test_extract_python_code_strips_fences() -> None:
    raw = "```python\ndef test_foo(): pass\n```"
    assert _extract_python_code(raw) == "def test_foo(): pass"


def test_extract_python_code_strips_generic_fence() -> None:
    raw = "```\ndef test_bar(): assert 1\n```"
    assert _extract_python_code(raw) == "def test_bar(): assert 1"


def test_extract_python_code_plain_passthrough() -> None:
    plain = "def test_baz():\n    assert True"
    assert _extract_python_code(plain) == plain


# ---------------------------------------------------------------------------
# _slug_from_title
# ---------------------------------------------------------------------------


def test_slug_from_title_basic() -> None:
    slug = _slug_from_title("Add authentication endpoint")
    assert slug == "add_authentication_endpoint"


def test_slug_from_title_strips_special_chars() -> None:
    slug = _slug_from_title("Fix bug: off-by-one in parser!")
    assert slug == "fix_bug_offbyone_in_parser"


def test_slug_from_title_empty() -> None:
    slug = _slug_from_title("")
    assert slug == "change"


def test_slug_from_title_long_truncated() -> None:
    slug = _slug_from_title("a" * 100)
    assert len(slug) <= 40


# ---------------------------------------------------------------------------
# generate_and_run — no Python changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_and_run_no_changes(tmp_path: Path) -> None:
    """When there is no diff, the gate skips and passes."""
    task = MagicMock()
    task.id = "task-abc"
    task.title = "Test task"
    task.description = "Some desc"

    config = IntegTestGenConfig(enabled=True)

    with patch("bernstein.core.integration_test_gen._get_diff", return_value=""):
        result = await generate_and_run(task, tmp_path, config)

    assert result.passed is True
    assert result.blocked is False
    assert "No Python changes" in result.detail


# ---------------------------------------------------------------------------
# generate_and_run — LLM failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_and_run_llm_failure(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "task-fail"
    task.title = "Fix bug"
    task.description = ""

    config = IntegTestGenConfig(enabled=True, block_on_fail=True)

    with (
        patch("bernstein.core.integration_test_gen._get_diff", return_value="--- a/foo.py\n+++ b/foo.py\n@@ def bar(): pass"),
        patch("bernstein.core.llm.call_llm", side_effect=RuntimeError("API error")),
    ):
        result = await generate_and_run(task, tmp_path, config)

    assert result.passed is False
    assert result.blocked is True
    assert "LLM error" in result.detail


# ---------------------------------------------------------------------------
# generate_and_run — LLM returns no test function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_and_run_no_test_function(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "task-notest"
    task.title = "Refactor"
    task.description = ""

    config = IntegTestGenConfig(enabled=True, block_on_fail=True)

    with (
        patch("bernstein.core.integration_test_gen._get_diff", return_value="diff --git a/x.py"),
        patch("bernstein.core.llm.call_llm", new_callable=AsyncMock, return_value="# no function here"),
    ):
        result = await generate_and_run(task, tmp_path, config)

    assert result.passed is False
    assert result.blocked is True
    assert "no valid test function" in result.detail


# ---------------------------------------------------------------------------
# generate_and_run — passing test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_and_run_passing_test(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "task-pass"
    task.title = "Add helper"
    task.description = ""

    # Create integration dir so temp file creation succeeds
    (tmp_path / "tests" / "integration").mkdir(parents=True)

    config = IntegTestGenConfig(enabled=True, block_on_fail=True)
    test_code = "def test_integration_add_helper():\n    assert 1 + 1 == 2\n"

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"1 passed", b""))

    with (
        patch("bernstein.core.integration_test_gen._get_diff", return_value="diff --git a/helper.py"),
        patch("bernstein.core.llm.call_llm", new_callable=AsyncMock, return_value=test_code),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        result = await generate_and_run(task, tmp_path, config)

    assert result.passed is True
    assert result.blocked is False
    assert "passed" in result.detail.lower()


# ---------------------------------------------------------------------------
# generate_and_run — failing test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_and_run_failing_test(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "task-fail2"
    task.title = "Add endpoint"
    task.description = ""

    (tmp_path / "tests" / "integration").mkdir(parents=True)

    config = IntegTestGenConfig(enabled=True, block_on_fail=True)
    test_code = "def test_integration_add_endpoint():\n    assert False\n"

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"FAILED", b""))

    with (
        patch("bernstein.core.integration_test_gen._get_diff", return_value="diff --git a/ep.py"),
        patch("bernstein.core.llm.call_llm", new_callable=AsyncMock, return_value=test_code),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        result = await generate_and_run(task, tmp_path, config)

    assert result.passed is False
    assert result.blocked is True
    assert "FAILED" in result.detail or "failed" in result.detail.lower()
