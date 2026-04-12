"""Backward-compatibility shim — moved to bernstein.core.security.denial_tracker."""
from bernstein.core.security.denial_tracker import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.denial_tracker")
def __getattr__(name: str):
    return getattr(_real, name)
