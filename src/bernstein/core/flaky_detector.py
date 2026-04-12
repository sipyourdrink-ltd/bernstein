"""Backward-compat shim: module moved to bernstein.core.quality.flaky_detector."""

from bernstein.core.quality.flaky_detector import *  # noqa: F401,F403
from bernstein.core.quality.flaky_detector import (  # noqa: F401
    _PYTEST_RESULT_RE,
    _WRITE_LOCK,
)
