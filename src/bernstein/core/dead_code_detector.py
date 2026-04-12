"""Backward-compat shim: module moved to bernstein.core.quality.dead_code_detector."""

from bernstein.core.quality.dead_code_detector import *  # noqa: F401,F403
from bernstein.core.quality.dead_code_detector import (  # noqa: F401
    _FUNC_DEF_RE,
    _CLASS_DEF_RE,
    _ADDED_FUNC_RE,
    _ADDED_CLASS_RE,
    _IGNORE_NAMES,
)
