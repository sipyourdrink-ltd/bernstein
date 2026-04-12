"""Backward-compatibility shim — moved to bernstein.core.tokens.token_binding."""
from bernstein.core.tokens.token_binding import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.token_binding")
def __getattr__(name: str):
    return getattr(_real, name)
