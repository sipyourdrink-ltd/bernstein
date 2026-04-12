"""Backward-compatibility shim — moved to bernstein.core.observability.error_classifier."""
from bernstein.core.observability.error_classifier import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.observability.error_classifier")
def __getattr__(name: str):
    return getattr(_real, name)
