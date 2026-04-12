"""Backward-compat shim: module moved to bernstein.core.quality.integration_test_gen."""

from bernstein.core.quality.integration_test_gen import *  # noqa: F401,F403
from bernstein.core.quality.integration_test_gen import (  # noqa: F401
    _DEFAULT_MODEL,
    _DEFAULT_PROVIDER,
    _MAX_DIFF_CHARS,
    _MAX_TOKENS,
    _TEST_TIMEOUT_S,
    _PROMPT_TEMPLATE,
)
