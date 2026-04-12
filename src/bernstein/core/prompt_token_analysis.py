"""Backward-compatibility shim — moved to bernstein.core.tokens.prompt_token_analysis."""
from bernstein.core.tokens.prompt_token_analysis import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.prompt_token_analysis")
def __getattr__(name: str):
    return getattr(_real, name)
