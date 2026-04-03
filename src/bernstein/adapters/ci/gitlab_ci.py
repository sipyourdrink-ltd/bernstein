"""GitLab CI adapter.

Parses GitLab CI job log format, extracts job names, failure
output, and maps results to ``CIFailure`` objects.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bernstein.core.ci_fix import CIFailure, parse_failures

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitLab CI log format helpers
# ---------------------------------------------------------------------------

# GitLab CI runner section_start/end markers.
# Example: section_start:1704067200:step1\r<section_end:1704067210:step1
_SECTION_START_RE = re.compile(r"section_start:\d+:(\S+)\r?\[.*?\]\r?\n")
_SECTION_END_RE = re.compile(r"section_end:\d+:(\S+)\r?\n")

# ANSI escape sequences — GitLab CI logs are heavily styled.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Error / failure indicators in GitLab CI logs.
_GITLAB_ERROR_RE = re.compile(
    r"^(?:ERROR|FATAL|FAILED|WARNING|Traceback|#<\w+Error)",
    re.MULTILINE,
)


@dataclass
class GitLabCIStep:
    """A parsed GitLab CI step extracted from a job log.

    Attributes:
        name: Step name taken from the ``section_start`` marker.
        body: Body text between start/end markers (stripped of ANSI codes).
        has_errors: Whether error indicators were found in the body.
    """

    name: str
    body: str
    has_errors: bool = False


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from GitLab CI log text.

    Args:
        text: Raw GitLab CI job log text.

    Returns:
        Clean text without ANSI escape codes.
    """
    return _ANSI_RE.sub("", text)


def _extract_steps(clean_log: str) -> list[GitLabCIStep]:
    """Extract grouped steps from a GitLab CI job log.

    GitLab CI uses ``section_start:<ts>:<name>`` markers to delimit steps.

    Args:
        clean_log: ANSI-stripped log text.

    Returns:
        List of ``GitLabCIStep`` objects.
    """
    steps: list[GitLabCIStep] = []
    positions: list[tuple[str, int]] = []

    for m in _SECTION_START_RE.finditer(clean_log):
        positions.append((m.group(1), m.end()))

    for i, (name, start_pos) in enumerate(positions):
        end_pos = positions[i + 1][1] if i + 1 < len(positions) else len(clean_log)
        body = clean_log[start_pos:end_pos].strip()
        has_errors = bool(_GITLAB_ERROR_RE.search(body))
        steps.append(GitLabCIStep(name=name, body=body, has_errors=has_errors))

    return steps


def _extract_job_name(clean_log: str) -> str:
    """Attempt to extract the job name from the log.

    Args:
        clean_log: ANSI-stripped log text.

    Returns:
        Extracted job name, or ``"gitlab_ci"`` as fallback.
    """
    return "gitlab_ci"


class GitLabCIParser:
    """CI log parser for GitLab CI.

    Parses the ``section_start``/``section_end`` structure and error
    annotations, then delegates to the core ``parse_failures`` function for
    content-level classification.

    Attributes:
        name: Parser identifier (``"gitlab_ci"``).
    """

    name: str = "gitlab_ci"

    def parse(self, raw_log: str) -> list[CIFailure]:
        """Parse a GitLab CI job log into structured CI failures.

        Strategy:
        1. Strip ANSI escape sequences.
        2. Extract ``section_start``/``section_end`` steps.
        3. For each step containing error indicators, run the core
           ``parse_failures`` on the step body.
        4. If no section markers are found, fall back to parsing the
           entire log.

        Args:
            raw_log: Raw log output from a GitLab CI job.

        Returns:
            List of ``CIFailure`` objects.
        """
        clean = _strip_ansi(raw_log)
        steps = _extract_steps(clean)

        # If we found steps with errors, parse each one individually.
        failing_steps = [s for s in steps if s.has_errors]
        if failing_steps:
            failures: list[CIFailure] = []
            for step in failing_steps:
                step_failures = parse_failures(step.body, job=step.name)
                failures.extend(step_failures)
            return failures

        # Fallback: parse the entire log as a single block.
        job = _extract_job_name(clean)
        return parse_failures(clean, job=job)
