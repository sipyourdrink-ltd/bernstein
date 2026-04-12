"""Backward-compatibility shim — moved to bernstein.core.security.security_correlation."""
from bernstein.core.security.security_correlation import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.security_correlation")
def __getattr__(name: str):
    return getattr(_real, name)
