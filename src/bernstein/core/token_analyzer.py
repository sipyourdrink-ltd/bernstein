"""Backward-compatibility shim — moved to bernstein.core.tokens.token_analyzer."""
from bernstein.core.tokens.token_analyzer import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.token_analyzer")
def __getattr__(name: str):
    return getattr(_real, name)
