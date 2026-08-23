"""Structured first-run error categorization for Bernstein.

This package exposes a closed taxonomy of failure categories, a categorizer
that maps any ``BaseException`` to a single category, and a Rich-formatted
hint renderer keyed off the category.

Public surface (importable from ``bernstein.core.errors``):

- :class:`ErrorCategory`: closed StrEnum of the 9 categories.
- :class:`BernsteinFirstRunError`: typed exception that carries a category.
- :class:`AdapterAuthError`: marker exception for adapter authentication
  failures (raised by adapters when an auth-failure exit code is observed).
- :class:`AdapterBinaryNotFoundError`: marker exception for missing adapter
  binaries on ``PATH`` (raised by the adapter registry).
- :func:`categorize_exception`: total function mapping any exception to a
  :class:`ErrorCategory`.
- :func:`exit_code_for`: total function mapping a category to its
  ``sysexits.h`` exit code.
- :func:`hint_for`: total function rendering a Rich :class:`Panel` for a
  category and an optional context dict.
- :func:`render_hint`: convenience wrapper that prints a hint and, when
  ``verbose=True``, the original traceback below.

The taxonomy is consumed by the opt-in telemetry subsystem via the
``error_category`` field on a first-run report.
"""

from __future__ import annotations

from bernstein.core.errors.categories import (
    AdapterAuthError,
    AdapterBinaryNotFoundError,
    BernsteinFirstRunError,
    ErrorCategory,
    exit_code_for,
)
from bernstein.core.errors.categorization import categorize_exception
from bernstein.core.errors.hints import HintContext, hint_for, render_hint

__all__ = [
    "AdapterAuthError",
    "AdapterBinaryNotFoundError",
    "BernsteinFirstRunError",
    "ErrorCategory",
    "HintContext",
    "categorize_exception",
    "exit_code_for",
    "hint_for",
    "render_hint",
]
