"""CI status monitor that polls GitHub Actions API for failing workflow runs.

Provides ``CIMonitor`` for discovering new CI failures and extracting
structured failure context from GitHub Actions logs.  The monitor uses
the GitHub REST API (via httpx) to list workflow runs and download
job logs, then parses them into ``FailureContext`` objects suitable for
the ``CIAutofixPipeline`` in ``ci_fix.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"

# Regex patterns for log parsing
_PYTEST_FAILURE_RE = re.compile(
    r"FAILED\s+([\w/.\-]+)::(\w+)",
)
_FILE_LINE_RE = re.compile(
    r"File\s+\"([^\"]+)\",\s+line\s+(\d+)",
)
_TRACEBACK_BLOCK_RE = re.compile(
    r"(Traceback[ \t]+\(most recent call last\):[\s\S]{0,5000})(?=\n\S|\Z)",
)
_ERROR_LINE_RE = re.compile(
    r"^((?:[\w.]*\w)?(?:Error|Exception|Failure)[^\n]*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class CIFailure:
    """A failing GitHub Actions workflow run.

    Attributes:
        run_id: GitHub Actions run identifier.
        workflow_name: Name of the workflow (e.g. ``"CI"``).
        branch: Git branch the run was triggered on.
        commit_sha: Full commit SHA that triggered the run.
        failure_url: HTML URL to the failing run.
        timestamp: ISO-8601 timestamp of the run creation.
    """

    run_id: int
    workflow_name: str
    branch: str
    commit_sha: str
    failure_url: str
    timestamp: str


@dataclass(frozen=True)
class FailureContext:
    """Structured context extracted from a CI failure log.

    Attributes:
        test_name: Fully qualified test name (e.g. ``tests/test_foo.py::test_bar``).
        error_message: The error/exception message.
        stack_trace: Full traceback text (may be empty if not found).
        file_path: Source file referenced in the traceback.
        line_number: Line number in the source file (0 if unknown).
    """

    test_name: str
    error_message: str
    stack_trace: str = ""
    file_path: str = ""
    line_number: int = 0


@dataclass
class CIMonitor:
    """Polls the GitHub Actions API for failing workflow runs.

    Usage::

        monitor = CIMonitor()
        failures = await monitor.poll_failures("owner/repo", token="ghp_...")
        for failure in failures:
            ctx = await monitor.parse_failure_logs(
                "owner/repo", failure.run_id, token="ghp_..."
            )
            print(ctx)

    Attributes:
        seen_run_ids: Set of run IDs already processed (prevents duplicates
            across poll cycles).
        base_url: GitHub API base URL (override for testing).
    """

    seen_run_ids: set[int] = field(default_factory=set)
    base_url: str = _GITHUB_API

    async def poll_failures(
        self,
        repo: str,
        token: str,
        *,
        per_page: int = 10,
    ) -> list[CIFailure]:
        """Poll GitHub Actions for recent failing workflow runs.

        Args:
            repo: Repository in ``owner/repo`` format.
            token: GitHub personal access token or app token.
            per_page: Number of recent runs to check.

        Returns:
            List of ``CIFailure`` objects for newly-discovered failures.
        """
        url = f"{self.base_url}/repos/{repo}/actions/runs"
        headers = _build_headers(token)
        params: dict[str, str | int] = {
            "status": "failure",
            "per_page": per_page,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        failures: list[CIFailure] = []
        for run in data.get("workflow_runs", []):
            run_id = int(run["id"])
            if run_id in self.seen_run_ids:
                continue
            self.seen_run_ids.add(run_id)
            failures.append(
                CIFailure(
                    run_id=run_id,
                    workflow_name=run.get("name", "unknown"),
                    branch=run.get("head_branch", ""),
                    commit_sha=run.get("head_sha", ""),
                    failure_url=run.get("html_url", ""),
                    timestamp=run.get("created_at", ""),
                )
            )
        return failures

    async def parse_failure_logs(
        self,
        repo: str,
        run_id: int,
        token: str,
    ) -> FailureContext:
        """Download and parse logs for a specific failing run.

        Fetches the combined log from the GitHub Actions API, then
        extracts the first test failure, traceback, and error message.

        Args:
            repo: Repository in ``owner/repo`` format.
            run_id: GitHub Actions workflow run ID.
            token: GitHub personal access token.

        Returns:
            Parsed ``FailureContext`` with as much detail as could be
            extracted from the log.
        """
        raw_log = await self._download_run_log(repo, run_id, token)
        return parse_log_to_context(raw_log)

    async def _download_run_log(
        self,
        repo: str,
        run_id: int,
        token: str,
    ) -> str:
        """Download the combined log text for a workflow run.

        Args:
            repo: Repository in ``owner/repo`` format.
            run_id: GitHub Actions workflow run ID.
            token: GitHub personal access token.

        Returns:
            Raw log text (may be large).

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        url = f"{self.base_url}/repos/{repo}/actions/runs/{run_id}/logs"
        headers = _build_headers(token)

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text


def parse_log_to_context(raw_log: str) -> FailureContext:
    """Parse a raw CI log into a ``FailureContext``.

    Extracts the first pytest FAILED line, the first traceback block,
    and the first ``File "...", line N`` reference.  Falls back to
    generic error extraction if no pytest failure is found.

    Args:
        raw_log: Raw CI log text.

    Returns:
        Populated ``FailureContext`` (fields may be empty if parsing
        finds nothing).
    """
    test_name = ""
    file_path = ""
    line_number = 0
    error_message = ""
    stack_trace = ""

    # 1. Try to find a pytest FAILED line
    m = _PYTEST_FAILURE_RE.search(raw_log)
    if m:
        test_name = f"{m.group(1)}::{m.group(2)}"

    # 2. Extract the first traceback block
    tb = _TRACEBACK_BLOCK_RE.search(raw_log)
    if tb:
        stack_trace = tb.group(1).strip()

    # 3. Extract file/line from the traceback
    fl = _FILE_LINE_RE.search(stack_trace or raw_log)
    if fl:
        file_path = fl.group(1)
        line_number = int(fl.group(2))

    # 4. Extract error message
    em = _ERROR_LINE_RE.search(raw_log)
    if em:
        error_message = em.group(1).strip()

    if not error_message and not test_name:
        snippet = raw_log[:500].strip()
        error_message = snippet if snippet else "Unknown CI failure"

    return FailureContext(
        test_name=test_name,
        error_message=error_message,
        stack_trace=stack_trace,
        file_path=file_path,
        line_number=line_number,
    )


def _build_headers(token: str) -> dict[str, str]:
    """Build GitHub API request headers.

    Args:
        token: GitHub personal access token.

    Returns:
        Headers dict with Authorization and Accept.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
