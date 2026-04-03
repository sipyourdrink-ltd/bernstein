"""CI system adapters for log parsing and failure extraction."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_built_in_ci_parsers() -> None:
    """Register all built-in CI log parsers at startup.

    Call this during bootstrap so that ``cifix --parser gitlab_ci``
    and pipeline self-healing both find their parsers without the caller
    having to import and register them manually.
    """

    from bernstein.adapters.ci.github_actions import GitHubActionsParser
    from bernstein.adapters.ci.gitlab_ci import GitLabCIParser
    from bernstein.core.ci_log_parser import register_parser

    register_parser(GitHubActionsParser())
    logger.debug("Registered CI log parser: github_actions")
    register_parser(GitLabCIParser())
    logger.debug("Registered CI log parser: gitlab_ci")
