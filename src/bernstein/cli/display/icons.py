"""Backward-compatibility shim.

The real module moved to ``bernstein.core.observability.icons`` to satisfy
the ``core-no-cli`` import-linter contract (core must not import cli/).
This shim keeps the old import path working for external callers.
"""

from __future__ import annotations

from bernstein.core.observability.icons import *  # noqa: F403
from bernstein.core.observability.icons import (
    _is_truthy as _is_truthy,
)
from bernstein.core.observability.icons import (
    get_icons as get_icons,
)
