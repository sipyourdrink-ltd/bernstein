"""Backward-compatibility shim — moved to bernstein.core.persistence.file_locks."""
from bernstein.core.persistence.file_locks import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.file_locks")
def __getattr__(name: str):
    return getattr(_real, name)
