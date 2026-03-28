"""<YourCI> log parser for Bernstein's CI self-healing pipeline.

Replace every occurrence of:
  - ``YourCI``         → the CI system name (e.g. ``GitLabCI``, ``CircleCI``)
  - ``your_ci``        → the parser key used in the registry (e.g. ``"gitlab"``)

Then delete these comments and open a PR!
"""

from __future__ import annotations

import re

from bernstein.core.ci_fix import CIFailure, parse_failures

# ---------------------------------------------------------------------------
# Log structure helpers
# ---------------------------------------------------------------------------
# Define regex patterns that match your CI system's log format.
#
# Examples from different CI systems:
#
#   GitHub Actions: "##[group]Step name" / "##[endgroup]" / "##[error]..."
#   GitLab CI:      "section_start:1700000000:step_name\r\033[0K..."
#   CircleCI:       "#!/bin/bash -eo pipefail\n..." with ANSI color codes
#   Jenkins:        "[Pipeline] stage" / "[ERROR] ..."

# TODO: Define patterns for your CI system's log structure
_SECTION_START_RE = re.compile(r"TODO: regex matching your CI's section/step start marker")
_SECTION_END_RE = re.compile(r"TODO: regex matching your CI's section/step end marker")
_ERROR_RE = re.compile(r"TODO: regex matching your CI's error annotation")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from log text (common in many CI systems).

    Args:
        text: Raw log text that may contain color codes.

    Returns:
        Clean text with escape sequences removed.
    """
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def _extract_sections(raw_log: str) -> list[tuple[str, str]]:
    """Extract named sections from the log as (name, body) pairs.

    Args:
        raw_log: Full log text (already stripped of ANSI codes).

    Returns:
        List of (section_name, section_body) tuples.
    """
    # TODO: implement section extraction for your CI system
    # Return a list of (name, body) tuples where name is the step/stage name
    # and body is the text content of that step.
    #
    # If your CI doesn't use structured sections, return the whole log
    # as a single section:
    #   return [("build", raw_log)]
    raise NotImplementedError("implement _extract_sections for your CI system")


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------


class YourCIParser:
    """CI log parser for <YourCI>.

    Implements the ``CILogParser`` protocol from
    ``bernstein.core.ci_log_parser`` — no import required, duck typing is used.

    Attributes:
        name: Parser key used in the registry. Must be unique across all parsers.
    """

    name: str = "your_ci"  # TODO: change to your CI system's key, e.g. "gitlab", "circleci"

    def parse(self, raw_log: str) -> list[CIFailure]:
        """Parse raw <YourCI> log output into structured CI failures.

        Strategy:
        1. Strip ANSI codes so content matchers work on clean text.
        2. Split log into named sections/steps.
        3. For each failing section, delegate to ``parse_failures`` for
           content-level classification (ruff, pytest, pyright, etc.).
        4. If no sections found, fall back to parsing the whole log.

        Args:
            raw_log: Full or partial log output from a <YourCI> run.

        Returns:
            List of ``CIFailure`` objects (may be empty if no failures found).
        """
        clean = _strip_ansi(raw_log)
        sections = _extract_sections(clean)

        if not sections:
            # Fallback: treat entire log as one block
            return parse_failures(clean, job="your_ci")

        failures: list[CIFailure] = []
        for section_name, section_body in sections:
            # TODO: filter to only failing sections if your CI marks them clearly
            # e.g.: if not _has_failure(section_body): continue
            section_failures = parse_failures(section_body, job=section_name)
            failures.extend(section_failures)

        return failures


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
# Register this parser so Bernstein's CI fix pipeline can use it.
# Add the following to src/bernstein/adapters/ci/__init__.py (or a new file):
#
#   from bernstein.adapters.ci.your_ci import YourCIParser
#   from bernstein.core.ci_log_parser import register_parser
#
#   register_parser(YourCIParser())
#
# You can also register it at runtime:
#   from bernstein.core.ci_log_parser import register_parser
#   from bernstein.adapters.ci.your_ci import YourCIParser
#   register_parser(YourCIParser())
