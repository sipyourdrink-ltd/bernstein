"""Backward-compat shim: module moved to bernstein.core.quality.fast_path."""

from bernstein.core.quality.fast_path import *  # noqa: F401,F403
from bernstein.core.quality.fast_path import (  # noqa: F401
    _EXECUTORS,
    _ACTION_MAP,
    _ESTIMATED_SAVINGS_PER_TASK_USD,
)
