"""Backward-compatibility shim — moved to bernstein.core.persistence.disaster_recovery."""
from bernstein.core.persistence.disaster_recovery import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.disaster_recovery")
def __getattr__(name: str):
    return getattr(_real, name)
