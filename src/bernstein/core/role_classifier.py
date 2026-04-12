"""Backward-compatibility shim — moved to bernstein.core.routing.role_classifier."""
from bernstein.core.routing.role_classifier import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.role_classifier")
def __getattr__(name: str):
    return getattr(_real, name)
