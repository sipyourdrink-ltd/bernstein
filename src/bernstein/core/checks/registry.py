"""Registry for govern audit checks (#5072).

Maintains an explicit, ordered registry of checks and coordinates their
execution with exception isolation so a throwing check is reported as a
``not_measurable`` finding carrying the exception class without aborting
the rest of the check suite.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bernstein.core.checks.contract import Check, Finding, Verdict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


class CheckRegistry:
    """Registry managing registered audit checks."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def register(self, check: Check) -> None:
        """Register a check.

        Raises:
            TypeError: If ``check`` does not conform to the :class:`Check` protocol.
            ValueError: If ``check.check_id`` is invalid, not namespaced, or already registered.
        """
        if not hasattr(check, "check_id") or not (hasattr(check, "run") or callable(check)):
            raise TypeError(f"Expected Check instance, got {type(check).__name__}")

        check_id = check.check_id
        if not check_id or not isinstance(check_id, str) or not check_id.strip():
            raise ValueError("check_id must be a non-empty string")

        if ":" not in check_id or check_id.startswith(":") or check_id.endswith(":"):
            raise ValueError(
                f"check_id '{check_id}' must be namespaced with a colon delimiter (e.g. 'namespace:check_name')"
            )

        if check_id in self._checks:
            raise ValueError(f"Check with id '{check_id}' is already registered")

        self._checks[check_id] = check

    def unregister(self, check_id: str) -> None:
        """Unregister a check by its ID."""
        self._checks.pop(check_id, None)

    def clear(self) -> None:
        """Clear all registered checks."""
        self._checks.clear()

    def get_check(self, check_id: str) -> Check | None:
        """Retrieve a registered check by ID."""
        return self._checks.get(check_id)

    def iter_checks(self) -> Iterator[Check]:
        """Iterate over registered checks in registration order."""
        yield from self._checks.values()

    def run_all(self, workdir: Path | None = None) -> list[Finding]:
        """Execute all registered checks against *workdir*.

        Catches exceptions raised by individual checks, recording them as
        ``not_measurable`` findings with the exception class name as reason,
        and proceeds to execute all remaining checks.
        """
        findings: list[Finding] = []
        for check in self._checks.values():
            try:
                finding = check.run(workdir)
            except Exception as exc:
                logger.warning(
                    "Check '%s' raised %s: %s; reporting as not_measurable",
                    check.check_id,
                    exc.__class__.__name__,
                    exc,
                )
                finding = Finding(
                    check_id=check.check_id,
                    verdict=Verdict.NOT_MEASURABLE,
                    what_would_make_it_measurable=f"{exc.__class__.__name__}: {exc}",
                    reason=exc.__class__.__name__,
                    message=f"Check execution failed with {exc.__class__.__name__}: {exc}",
                )
            findings.append(finding)
        return findings


# ---------------------------------------------------------------------------
# Global default registry and module-level conveniences
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY = CheckRegistry()


def register(check: Check) -> None:
    """Register a check in the default registry."""
    _DEFAULT_REGISTRY.register(check)


def unregister(check_id: str) -> None:
    """Unregister a check from the default registry."""
    _DEFAULT_REGISTRY.unregister(check_id)


def clear() -> None:
    """Clear the default registry."""
    _DEFAULT_REGISTRY.clear()


def get_check(check_id: str) -> Check | None:
    """Retrieve a check from the default registry."""
    return _DEFAULT_REGISTRY.get_check(check_id)


def iter_checks() -> Iterator[Check]:
    """Iterate over checks in the default registry."""
    return _DEFAULT_REGISTRY.iter_checks()


def run_all(workdir: Path | None = None) -> list[Finding]:
    """Execute all checks in the default registry."""
    return _DEFAULT_REGISTRY.run_all(workdir)
