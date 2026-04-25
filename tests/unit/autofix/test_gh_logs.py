"""Unit tests for the gh log extractor."""

from __future__ import annotations

import subprocess
from typing import Any

from bernstein.core.autofix.gh_logs import extract_failed_log


class _StubRunner:
    """Test seam that mimics :func:`subprocess.run` deterministically."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.calls: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_extract_returns_truncated_log_within_byte_budget() -> None:
    """Long logs are head-truncated to the byte budget."""
    runner = _StubRunner(stdout="A" * 4096)
    result = extract_failed_log("12345", byte_budget=1024, runner=runner)
    assert result.ok is True
    assert result.truncated is True
    assert len(result.body.encode("utf-8")) <= 1024


def test_extract_passes_through_short_log_unchanged() -> None:
    """A log shorter than the budget is returned verbatim."""
    runner = _StubRunner(stdout="error: tiny")
    result = extract_failed_log("12345", byte_budget=10_000, runner=runner)
    assert result.ok is True
    assert result.truncated is False
    assert result.body == "error: tiny"


def test_extract_includes_repo_flag_when_supplied() -> None:
    """``-R owner/name`` is forwarded to gh when provided."""
    runner = _StubRunner(stdout="ok")
    extract_failed_log("99", byte_budget=1024, repo="owner/name", runner=runner)
    assert runner.calls
    assert "-R" in runner.calls[0]
    assert "owner/name" in runner.calls[0]


def test_extract_surfaces_gh_failure() -> None:
    """A non-zero gh exit yields ok=False with stderr captured."""
    runner = _StubRunner(returncode=1, stderr="auth required")
    result = extract_failed_log("1", byte_budget=1024, runner=runner)
    assert result.ok is False
    assert "auth required" in result.error


def test_extract_surfaces_timeout() -> None:
    """A subprocess timeout yields ok=False with a clear message."""
    runner = _StubRunner(timeout=True)
    result = extract_failed_log("1", byte_budget=1024, runner=runner, timeout_seconds=0.1)
    assert result.ok is False
    assert "timed out" in result.error
