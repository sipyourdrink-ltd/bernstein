"""Closed taxonomy of first-run failure categories.

Every first-run failure surfaces as one of these eight categories.  The
categories map one-to-one to ``sysexits.h`` exit codes so CI matrices can
parse failure modes deterministically without scraping stderr.

The mapping intentionally mirrors the canonical BSD ``sysexits.h`` integers
so operators reading ``echo $?`` get the same signal across tooling.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ErrorCategory(StrEnum):
    """Closed set of first-run failure categories.

    Each category names a single, actionable failure mode the user can
    resolve from the hint alone.  The set is closed: an uncategorisable
    failure is mapped to :attr:`UNKNOWN` rather than added to the enum.
    """

    CONFIG_MISSING = "config_missing"
    AUTH_FAILED = "auth_failed"
    DEPENDENCY_MISSING = "dependency_missing"
    MODEL_UNREACHABLE = "model_unreachable"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    PORT_CONFLICT = "port_conflict"
    NO_PLAN_PRODUCED = "no_plan_produced"
    UNKNOWN = "unknown"


# ``sysexits.h`` exit code per category.  These constants are duplicated
# here (rather than importing from the host's ``sysexits.h``) because the
# values are stable across the BSD tradition and Python's ``os.EX_*``
# bindings are not available on Windows.
_EX_DATAERR: Final[int] = 65
_EX_NOHOST: Final[int] = 68
_EX_UNAVAILABLE: Final[int] = 69
_EX_SOFTWARE: Final[int] = 70
_EX_TEMPFAIL: Final[int] = 75
_EX_NOPERM: Final[int] = 77


_EXIT_CODES: Final[dict[ErrorCategory, int]] = {
    ErrorCategory.CONFIG_MISSING: _EX_DATAERR,
    ErrorCategory.AUTH_FAILED: _EX_NOPERM,
    ErrorCategory.DEPENDENCY_MISSING: _EX_UNAVAILABLE,
    ErrorCategory.MODEL_UNREACHABLE: _EX_NOHOST,
    ErrorCategory.TIMEOUT: _EX_TEMPFAIL,
    ErrorCategory.PERMISSION_DENIED: _EX_NOPERM,
    ErrorCategory.PORT_CONFLICT: _EX_TEMPFAIL,
    ErrorCategory.NO_PLAN_PRODUCED: _EX_SOFTWARE,
    ErrorCategory.UNKNOWN: _EX_SOFTWARE,
}


def exit_code_for(category: ErrorCategory) -> int:
    """Return the ``sysexits.h`` exit code for a category.

    Args:
        category: A member of :class:`ErrorCategory`.

    Returns:
        The integer exit code in the inclusive sysexits range ``[64, 78]``.
    """
    return _EXIT_CODES[category]


class BernsteinFirstRunError(Exception):
    """Typed first-run failure that carries a structured category.

    Worker bootstrap and CLI entrypoints raise this when they can attach
    structural meaning to a failure that would otherwise surface as a bare
    ``RuntimeError`` or untyped ``sys.exit``.

    Attributes:
        category: The structured :class:`ErrorCategory` for this failure.
        context: Optional dict of hint-rendering context (e.g. adapter
            name, env var, port number).
    """

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.context: dict[str, object] = dict(context or {})

    def __repr__(self) -> str:
        return f"BernsteinFirstRunError(category={self.category.value!r}, message={str(self)!r})"


class AdapterAuthError(Exception):
    """Adapter spawn reported an authentication failure.

    Raised by adapter wrappers when the spawned CLI agent returns an exit
    code that matches the auth-failure pattern (e.g. Anthropic 401, OpenAI
    invalid-key) or prints a recognised auth-failure marker.

    Attributes:
        adapter: Name of the adapter (e.g. ``claude``, ``codex``).
        env_var: Environment variable the user should set, if known.
    """

    def __init__(
        self,
        message: str,
        *,
        adapter: str = "",
        env_var: str = "",
    ) -> None:
        super().__init__(message)
        self.adapter = adapter
        self.env_var = env_var


class AdapterBinaryNotFoundError(Exception):
    """Adapter is configured but its binary is missing from ``PATH``.

    Raised by the adapter registry when it cannot resolve a configured
    adapter to an executable on disk.

    Attributes:
        adapter: Name of the adapter (e.g. ``claude``).
        install_hint: Short suggested install command, if known.
    """

    def __init__(
        self,
        message: str,
        *,
        adapter: str = "",
        install_hint: str = "",
    ) -> None:
        super().__init__(message)
        self.adapter = adapter
        self.install_hint = install_hint
