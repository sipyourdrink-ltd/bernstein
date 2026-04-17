"""CI system adapters for log parsing and failure extraction.

Importing this package registers every built-in CI log parser with the
global registry (:mod:`bernstein.core.ci_log_parser`). The registration
is idempotent, so calling :func:`register_built_in_ci_parsers` again —
as :mod:`bernstein.core.orchestration.bootstrap` does explicitly — has
no additional effect.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register_built_in_ci_parsers"]

# Track whether the built-ins have already been registered in this process
# so import-time and explicit bootstrap calls do not double-register.
_BUILTINS_REGISTERED = False


def register_built_in_ci_parsers() -> None:
    """Register all built-in CI log parsers with the global registry.

    Call this during bootstrap so that ``cifix --parser gitlab_ci``
    and pipeline self-healing both find their parsers without the caller
    having to import and register them manually. Repeat calls are a
    no-op — the registry keys on parser ``name`` so re-registering the
    same parser would only overwrite itself with an equivalent instance,
    but we guard with a module-level flag to avoid needless work.
    """
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return

    from bernstein.adapters.ci.github_actions import GitHubActionsParser
    from bernstein.adapters.ci.gitlab_ci import GitLabCIParser
    from bernstein.core.ci_log_parser import register_parser

    register_parser(GitHubActionsParser())
    logger.debug("Registered CI log parser: github_actions")
    register_parser(GitLabCIParser())
    logger.debug("Registered CI log parser: gitlab_ci")

    _BUILTINS_REGISTERED = True


# Register built-ins at import time so ``from bernstein.adapters.ci import ...``
# (or any transitive import via ``bernstein.core.quality.ci_fix``) is enough
# to populate the registry. The explicit call in bootstrap remains for clarity
# and for code paths that never import this package directly.
register_built_in_ci_parsers()
