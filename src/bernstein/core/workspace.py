"""Backward-compatibility shim — moved to bernstein.core.persistence.workspace."""
from bernstein.core.persistence.workspace import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.workspace")
def __getattr__(name: str):
    return getattr(_real, name)
