"""Generic CI log parser with adapter pattern.

Defines the ``CILogParser`` protocol and provides a registry so new CI
systems (GitHub Actions, GitLab CI, CircleCI, etc.) can be plugged in
without touching the core pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bernstein.core.ci_fix import CIFailure

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CILogParser(Protocol):
    """Protocol for CI log parsers.

    Each CI system adapter implements this protocol to convert raw log
    output into a list of ``CIFailure`` objects the fix pipeline can act on.
    """

    name: str
    """Human-readable name of the CI system (e.g. ``"github_actions"``)."""

    def parse(self, raw_log: str) -> list[CIFailure]:
        """Parse raw CI log text into structured failures.

        Args:
            raw_log: Full or partial log output from a CI run.

        Returns:
            List of parsed ``CIFailure`` objects (may be empty).
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PARSERS: dict[str, CILogParser] = {}


def register_parser(parser: CILogParser) -> None:
    """Register a CI log parser by its ``name`` attribute.

    Args:
        parser: A parser instance implementing ``CILogParser``.
    """
    _PARSERS[parser.name] = parser
    logger.debug("Registered CI log parser: %s", parser.name)


def get_parser(name: str) -> CILogParser | None:
    """Retrieve a registered parser by name.

    Args:
        name: The parser name (e.g. ``"github_actions"``).

    Returns:
        The parser instance, or ``None`` if not registered.
    """
    return _PARSERS.get(name)


def list_parsers() -> list[str]:
    """Return names of all registered parsers.

    Returns:
        Sorted list of parser names.
    """
    return sorted(_PARSERS)
