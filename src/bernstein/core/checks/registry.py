"""Registry for govern audit checks (#5072).

Maintains an explicit, ordered registry of checks and coordinates their
execution with exception isolation so a throwing check is reported as a
``not_measurable`` finding carrying the exception class without aborting
the rest of the check suite.
"""

from __future__ import annotations

import logging
import re
from threading import RLock
from typing import TYPE_CHECKING

from bernstein.core.checks.contract import Finding, Verdict
from bernstein.core.registry_guard import DuplicateGuard

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from bernstein.core.checks.contract import Check

logger = logging.getLogger(__name__)


def check_area(check: Check) -> str:
    """Derive the area for a check from its explicit attribute or namespaced check_id."""
    return getattr(check, "area", "") or (check.check_id.split(":", 1)[0] if ":" in check.check_id else "")


_check_area = check_area  # deprecated: back-compat alias; use check_area


class CheckRegistry:
    """Registry managing registered audit checks."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}
        self._guard = DuplicateGuard("audit check")
        self._lock = RLock()

    def register(self, check: Check) -> None:
        """Register a check.

        Raises:
            TypeError: If ``check`` does not conform to the :class:`Check` protocol or ``check_id`` is not a string.
            ValueError: If ``check.check_id`` is invalid (neither namespaced nor INS-nnn), or already registered.
        """
        if isinstance(check, type):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"Expected Check instance, got class '{check.__name__}'")
        if not hasattr(check, "check_id") or not hasattr(check, "run"):
            raise TypeError(f"Expected Check instance, got {type(check).__name__}")

        check_id = check.check_id
        if not isinstance(check_id, str):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"check_id must be a string, got {type(check_id).__name__}")
        if not check_id or not check_id.strip():
            raise ValueError("check_id must be a non-empty string")

        installation_id = re.fullmatch(r"INS-\d{3}", check_id) is not None
        if not installation_id and (":" not in check_id or check_id.startswith(":") or check_id.endswith(":")):
            raise ValueError(f"check_id '{check_id}' must be namespaced with a colon delimiter or use INS-nnn")

        module = type(check).__module__
        provenance = module if isinstance(module, str) and module else "<unknown>"
        with self._lock:
            self._guard.register(check_id, provenance)
            self._checks[check_id] = check

    def unregister(self, check_id: str) -> None:
        """Unregister a check by its ID."""
        with self._lock:
            self._checks.pop(check_id, None)
            self._guard.forget(check_id)

    def clear(self) -> None:
        """Clear all registered checks."""
        with self._lock:
            self._checks.clear()
            self._guard = DuplicateGuard("audit check")

    def get_check(self, check_id: str) -> Check | None:
        """Retrieve a registered check by ID."""
        return self._checks.get(check_id)

    def iter_checks(
        self,
        only_areas: Sequence[str] | None = None,
        skip_ids: Sequence[str] | None = None,
        *,
        include_opt_in: bool = False,
    ) -> Iterator[Check]:
        """Iterate over registered checks in registration order with optional filtering.

        A check with a truthy ``opt_in`` attribute runs only when *only_areas*
        is given (and names its area), or when *include_opt_in* asks for every
        check, as ``--list`` does. Such a check answers a question the audited
        workspace cannot change, so it must not turn a clean workspace's audit red.
        """
        only_set = {a.lower() for a in only_areas} if only_areas else None
        skip_set = {s.lower() for s in skip_ids} if skip_ids else None

        for check in self._checks.values():
            check_id = check.check_id
            if skip_set and check_id.lower() in skip_set:
                continue

            area = _check_area(check)
            if only_set and area.lower() not in only_set:
                continue

            if only_set is None and not include_opt_in and bool(getattr(check, "opt_in", False)):
                continue

            yield check

    def list_checks(
        self,
        only_areas: Sequence[str] | None = None,
        skip_ids: Sequence[str] | None = None,
    ) -> list[Check]:
        """Return a list of registered checks matching optional filters, opt-in checks included."""
        return list(self.iter_checks(only_areas=only_areas, skip_ids=skip_ids, include_opt_in=True))

    def run_all(
        self,
        workdir: Path | None = None,
        only_areas: Sequence[str] | None = None,
        skip_ids: Sequence[str] | None = None,
        *,
        on_error: Callable[[str], None] | None = None,
        log_errors: bool = True,
    ) -> list[Finding]:
        """Execute registered checks against *workdir* with optional filtering.

        Catches exceptions raised by individual checks, recording them as
        ``not_measurable`` findings with the exception class name as reason,
        and proceeds to execute all remaining checks. ``on_error`` receives
        each crashed check ID so callers can distinguish execution errors from
        expected missing prerequisites. ``log_errors=False`` silences warnings.
        """
        findings: list[Finding] = []
        for check in self.iter_checks(only_areas=only_areas, skip_ids=skip_ids):
            try:
                finding = check.run(workdir)
                if type(finding) is not Finding:
                    raise TypeError(
                        f"Check '{check.check_id}' run() returned {type(finding).__name__}, expected Finding"
                    )
            except Exception as exc:
                if on_error is not None:
                    on_error(check.check_id)
                if log_errors:
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

DEFAULT_REGISTRY = CheckRegistry()
_DEFAULT_REGISTRY = DEFAULT_REGISTRY


def populate_default_checks(registry: CheckRegistry | None = None) -> None:
    """Populate built-in check adapters into the given or default registry."""
    from bernstein.core.checks.adapters import (
        DUPLICATION_CHECK_NAMES,
        AuditSentinelCheck,
        ComplianceEncryptionAtRestAdapter,
        DeclarationQuarantineAdapter,
        DoctorComplianceAdapter,
        DuplicationAuditAdapter,
    )

    reg = DEFAULT_REGISTRY if registry is None else registry
    adapters: list[Check] = [
        DoctorComplianceAdapter(),
        ComplianceEncryptionAtRestAdapter(),
        DeclarationQuarantineAdapter(),
        AuditSentinelCheck(),
    ]
    adapters.extend(DuplicationAuditAdapter(name) for name in DUPLICATION_CHECK_NAMES)
    for adapter in adapters:
        if reg.get_check(adapter.check_id) is None:
            reg.register(adapter)


def register(check: Check) -> None:
    """Register a check in the default registry."""
    DEFAULT_REGISTRY.register(check)


def unregister(check_id: str) -> None:
    """Unregister a check from the default registry."""
    DEFAULT_REGISTRY.unregister(check_id)


def clear() -> None:
    """Clear the default registry."""
    DEFAULT_REGISTRY.clear()


def get_check(check_id: str) -> Check | None:
    """Retrieve a check from the default registry."""
    return DEFAULT_REGISTRY.get_check(check_id)


def iter_checks(
    only_areas: Sequence[str] | None = None,
    skip_ids: Sequence[str] | None = None,
) -> Iterator[Check]:
    """Iterate over checks in the default registry."""
    return DEFAULT_REGISTRY.iter_checks(only_areas=only_areas, skip_ids=skip_ids)


def list_checks(
    only_areas: Sequence[str] | None = None,
    skip_ids: Sequence[str] | None = None,
) -> list[Check]:
    """Return a list of checks in the default registry matching filters."""
    return DEFAULT_REGISTRY.list_checks(only_areas=only_areas, skip_ids=skip_ids)


def run_all(
    workdir: Path | None = None,
    only_areas: Sequence[str] | None = None,
    skip_ids: Sequence[str] | None = None,
) -> list[Finding]:
    """Execute all checks in the default registry matching filters."""
    return DEFAULT_REGISTRY.run_all(workdir=workdir, only_areas=only_areas, skip_ids=skip_ids)
