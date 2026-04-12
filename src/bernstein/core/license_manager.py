"""Backward-compatibility shim — moved to bernstein.core.security.license_manager."""
from bernstein.core.security.license_manager import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.license_manager")
def __getattr__(name: str):
    return getattr(_real, name)
