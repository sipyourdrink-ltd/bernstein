"""Backward-compatibility shim — moved to bernstein.core.git.github."""
from bernstein.core.git.github import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.github")
def __getattr__(name: str):
    return getattr(_real, name)
