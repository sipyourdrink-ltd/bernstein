"""Backward-compat shim: module moved to bernstein.core.quality.comment_quality."""

from bernstein.core.quality.comment_quality import *  # noqa: F401,F403
from bernstein.core.quality.comment_quality import (  # noqa: F401
    _GOOGLE_SECTION_RE,
    _GOOGLE_PARAM_RE,
    _NUMPY_SECTION_RE,
    _NUMPY_PARAM_RE,
    _REST_PARAM_RE,
    _REST_RETURNS_RE,
    _REST_RAISES_RE,
    _TRIVIAL_VERBS,
)
