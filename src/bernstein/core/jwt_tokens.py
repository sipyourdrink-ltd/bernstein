"""Backward-compatibility shim — moved to bernstein.core.security.jwt_tokens."""
from bernstein.core.security.jwt_tokens import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.jwt_tokens")
def __getattr__(name: str):
    return getattr(_real, name)
