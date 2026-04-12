"""Backward-compat shim: module moved to bernstein.core.quality.cross_model_verifier."""

from bernstein.core.quality.cross_model_verifier import *  # noqa: F401,F403
from bernstein.core.quality.cross_model_verifier import (  # noqa: F401
    _MAX_DIFF_CHARS,
    _MAX_TOKENS,
    _PROVIDER,
    _REVIEWER_GEMINI_FLASH,
    _REVIEWER_CLAUDE_HAIKU,
    _DEFAULT_REVIEWER,
    _WRITER_TO_REVIEWER,
    _REVIEW_PROMPT_TEMPLATE,
)
