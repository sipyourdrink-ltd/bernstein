"""GitHub Actions CI adapter.

Parses GitHub Actions log format, extracts job/step names and failure
output, and maps results to ``CIFailure`` objects.  Supports log download
via the ``gh`` CLI and the GitHub REST API.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

from bernstein.core.ci_fix import CIFailure, parse_failures

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub Actions log structure helpers
# ---------------------------------------------------------------------------

# Matches the timestamp prefix on every GHA log line.
# Example: 2024-01-15T10:30:00.0000000Z ##[group]Run ruff check src/
_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", re.MULTILINE)

# Matches ##[group]<name> / ##[endgroup] blocks.
_GROUP_RE = re.compile(
    r"##\[group\](.+?)$\n(.*?)##\[endgroup\]",
    re.MULTILINE | re.DOTALL,
)

# Matches ##[error] annotations.
_ERROR_RE = re.compile(r"##\[error\](.+?)$", re.MULTILINE)


@dataclass
class GHAStep:
    """A parsed GitHub Actions step extracted from the log.

    Attributes:
        name: Step name taken from the ``##[group]`` marker.
        body: Body text between group/endgroup markers (stripped of timestamps).
        errors: Any ``##[error]`` annotations found in the body.
    """

    name: str
    body: str
    errors: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _strip_timestamps(text: str) -> str:
    """Remove the ISO-8601 timestamp prefix from every log line.

    Args:
        text: Raw GitHub Actions log text.

    Returns:
        Log text with timestamps removed.
    """
    return _TS_PREFIX_RE.sub("", text)


def _extract_steps(raw_log: str) -> list[GHAStep]:
    """Extract grouped steps from a GitHub Actions log.

    Args:
        raw_log: Full raw log (timestamps already stripped).

    Returns:
        List of ``GHAStep`` objects.
    """
    steps: list[GHAStep] = []
    for m in _GROUP_RE.finditer(raw_log):
        name = m.group(1).strip()
        body = m.group(2).strip()
        errors = _ERROR_RE.findall(body)
        steps.append(GHAStep(name=name, body=body, errors=errors))
    return steps


def _extract_job_name(raw_log: str) -> str:
    """Attempt to extract the job name from the log header.

    GitHub Actions logs often start with a line like:
        ``##[group]Run <job-name>``

    Args:
        raw_log: Raw (timestamp-stripped) log.

    Returns:
        Extracted job name, or ``"github_actions"`` as fallback.
    """
    m = re.search(r"##\[group\]Run\s+(.+?)$", raw_log, re.MULTILINE)
    if m:
        return m.group(1).strip()[:80]
    return "github_actions"


class GitHubActionsParser:
    """CI log parser for GitHub Actions.

    Parses the ``##[group]`` / ``##[endgroup]`` structure and ``##[error]``
    annotations, then delegates to the core ``parse_failures`` function for
    content-level classification.

    Attributes:
        name: Parser identifier (``"github_actions"``).
    """

    name: str = "github_actions"

    def parse(self, raw_log: str) -> list[CIFailure]:
        """Parse a GitHub Actions log into structured CI failures.

        Strategy:
        1. Strip timestamps so content matchers work on clean text.
        2. Extract ``##[group]``/``##[endgroup]`` steps.
        3. For each step that contains ``##[error]`` annotations, run the
           core ``parse_failures`` on the step body.
        4. If no steps are found (log may not use group markers), fall back
           to parsing the whole log.

        Args:
            raw_log: Raw log output from a GitHub Actions run.

        Returns:
            List of ``CIFailure`` objects.
        """
        clean = _strip_timestamps(raw_log)
        steps = _extract_steps(clean)

        # If we found steps with errors, parse each one individually.
        failing_steps = [s for s in steps if s.errors]
        if failing_steps:
            failures: list[CIFailure] = []
            for step in failing_steps:
                step_failures = parse_failures(step.body, job=step.name)
                failures.extend(step_failures)
            return failures

        # Fallback: parse the entire log as a single block.
        job = _extract_job_name(clean)
        return parse_failures(clean, job=job)


# ---------------------------------------------------------------------------
# Log download
# ---------------------------------------------------------------------------


def download_github_actions_log(
    run_url: str,
    *,
    timeout: int = 60,
) -> str:
    """Download the failed-step log from a GitHub Actions run.

    Uses ``gh run view --log-failed`` which requires the ``gh`` CLI to be
    installed and authenticated.

    Args:
        run_url: URL of the GitHub Actions run, e.g.
            ``https://github.com/owner/repo/actions/runs/123456``.
        timeout: Subprocess timeout in seconds.

    Returns:
        Raw log text from the failed steps.

    Raises:
        RuntimeError: If the ``gh`` command fails.
    """
    # Extract the run ID from the URL.
    run_id = _extract_run_id(run_url)

    result = subprocess.run(
        ["gh", "run", "view", run_id, "--log-failed"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        msg = f"gh run view failed (exit {result.returncode}): {result.stderr.strip()[:200]}"
        raise RuntimeError(msg)
    return result.stdout


def download_github_actions_log_api(
    run_url: str,
    *,
    timeout: int = 60,
) -> str:
    """Download the failed-step log via ``gh api``.

    This is an alternative to ``download_github_actions_log`` that uses the
    GitHub REST API through ``gh api``, which can be more reliable in some
    environments.

    Args:
        run_url: URL of the GitHub Actions run.
        timeout: Subprocess timeout in seconds.

    Returns:
        Raw log text (JSON) from the API.

    Raises:
        RuntimeError: If the ``gh`` command fails.
    """
    run_id = _extract_run_id(run_url)

    # First get the jobs for this run.
    # Extract owner/repo from the URL.
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/actions/runs/\d+", run_url)
    if not m:
        msg = f"Cannot parse owner/repo from URL: {run_url}"
        raise ValueError(msg)
    repo = m.group(1)

    # Get failed jobs.
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs/{run_id}/jobs",
            "--jq",
            '.jobs[] | select(.conclusion == "failure") | .id',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        msg = f"gh api failed (exit {result.returncode}): {result.stderr.strip()[:200]}"
        raise RuntimeError(msg)

    job_ids = result.stdout.strip().splitlines()
    if not job_ids:
        return ""

    # Download logs for each failed job.
    logs: list[str] = []
    for job_id in job_ids:
        log_result = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if log_result.returncode == 0:
            logs.append(log_result.stdout)

    return "\n".join(logs)


def _extract_run_id(run_url: str) -> str:
    """Extract the numeric run ID from a GitHub Actions URL.

    Args:
        run_url: URL like ``https://github.com/owner/repo/actions/runs/123456``.

    Returns:
        The run ID as a string.

    Raises:
        ValueError: If the URL does not match the expected pattern.
    """
    m = re.search(r"/actions/runs/(\d+)", run_url)
    if not m:
        msg = f"Cannot extract run ID from URL: {run_url}"
        raise ValueError(msg)
    return m.group(1)
