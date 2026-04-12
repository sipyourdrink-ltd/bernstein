"""Backward-compatibility shim — moved to bernstein.core.tokens.cascading_token_counter."""
from bernstein.core.tokens.cascading_token_counter import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.cascading_token_counter")
def __getattr__(name: str):
    return getattr(_real, name)
