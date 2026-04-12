"""Backward-compat shim: module moved to bernstein.core.quality.quality_gates."""

from bernstein.core.quality.quality_gates import *  # noqa: F401,F403
from bernstein.core.quality.quality_gates import (  # noqa: F401
    _INTENT_MAX_DIFF_CHARS,
    _INTENT_MAX_TOKENS,
    _INTENT_DEFAULT_MODEL,
    _INTENT_PROVIDER,
    _FORK_CONTEXT_MAX_CHARS,
    _INTENT_PROMPT_TEMPLATE,
    _TEST_FILE_PATTERN,
    _SOURCE_FROM_TEST,
)
