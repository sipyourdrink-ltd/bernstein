"""Backward-compatibility shim — moved to bernstein.core.tokens.token_estimation."""
from bernstein.core.tokens.token_estimation import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.token_estimation")
def __getattr__(name: str):
    return getattr(_real, name)
