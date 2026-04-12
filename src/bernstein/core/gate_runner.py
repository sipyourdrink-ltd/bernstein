"""Backward-compat shim: module moved to bernstein.core.quality.gate_runner."""

from bernstein.core.quality.gate_runner import *  # noqa: F401,F403
from bernstein.core.quality.gate_runner import (  # noqa: F401
    _NO_PYTHON_FILES,
    _TIMED_OUT_PREFIX,
    _DEP_FILE_NAMES,
    _DEP_FILE_PREFIXES,
)
