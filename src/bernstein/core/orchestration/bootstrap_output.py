"""Error output selected by the caller for the duration of bootstrap."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

plain_bootstrap_stderr: ContextVar[Console | None] = ContextVar("plain_bootstrap_stderr", default=None)
