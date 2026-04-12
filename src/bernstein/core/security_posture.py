"""Backward-compatibility shim — moved to bernstein.core.security.security_posture."""
from bernstein.core.security.security_posture import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.security_posture")
def __getattr__(name: str):
    return getattr(_real, name)
