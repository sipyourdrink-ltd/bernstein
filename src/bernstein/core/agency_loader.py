"""Backward-compatibility shim — moved to bernstein.core.plugins_core.agency_loader."""
from bernstein.core.plugins_core.agency_loader import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.plugins_core.agency_loader")
def __getattr__(name: str):
    return getattr(_real, name)
