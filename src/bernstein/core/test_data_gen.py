"""Backward-compat shim: module moved to bernstein.core.quality.test_data_gen."""

from bernstein.core.quality.test_data_gen import *  # noqa: F401,F403
from bernstein.core.quality.test_data_gen import (  # noqa: F401
    _ROLES,
    _COMPLEXITY_FILE_COUNTS,
    _COMPLEXITY_DEPENDENCY_COUNTS,
    _COMPLEXITY_GATE_COUNTS,
    _COMPLEXITY_PRIORITIES,
    _QUALITY_GATES_ALL,
    _TASK_PREFIXES,
    _TASK_NOUNS,
    _TASK_MODIFIERS,
)
