"""Backward-compatibility shim — moved to bernstein.core.persistence.session_checkpoint."""
from bernstein.core.persistence.session_checkpoint import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.session_checkpoint")
def __getattr__(name: str):
    return getattr(_real, name)
