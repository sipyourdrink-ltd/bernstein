"""Backward-compatibility shim — moved to bernstein.core.security.hipaa."""
from bernstein.core.security.hipaa import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.hipaa")
def __getattr__(name: str):
    return getattr(_real, name)
